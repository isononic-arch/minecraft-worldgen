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
        # S89 walk: user — NO podzol; all podzol -> grass_block.
        ("grass_block",       "dirt",         "base"),
        ("grass_block",       "rooted_dirt",  "eco_moist"),       # moisture corridors
        ("grass_block",       "granite",      "eco_dry"),
        ("granite",           "stone",        "eco_shallow_soil"),# thin soil over rock
        ("dirt_path",         "dirt",         "noise"),
        ("grass_block",       "granite",      "erosion"),
        ("gravel",            "granite",      "erosion2"),
        ("granite",           "stone",        "noise2"),
        ("grass_block",       "dirt",         "moisture"),
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
        ("short_grass", 0.60), ("dead_bush", 0.02),   # S89 walk: user — way more short grass (was 0.30)
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
        # S89 walk3: user — crazy-lush birch floor.
        ("short_grass", 0.90), ("tall_grass", 0.35), ("leaf_litter", 0.45),
        ("bush", 0.38), ("fern", 0.45),
        ("lily_of_the_valley", 0.05), ("azalea", 0.07),
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
    # S87 walk #3: bumped across the board per user (13,82) walk.
    "RAINFOREST_COAST": [
        # S89 walk3: user — WAY more (jungle-floor lush).
        ("fern", 0.95), ("large_fern", 0.48), ("tall_grass", 0.35),
        ("bush", 0.42), ("short_grass", 0.45), ("moss_carpet", 0.58),
        ("leaf_litter", 0.45),
        # S60 damp-woodland flowers (very rare)
        ("azure_bluet", 0.01), ("lily_of_the_valley", 0.01),
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
        ("dead_bush", 0.02), ("bush", 0.08), ("short_grass", 0.40),  # S89 walk2: living grass up
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
    # S87 REVERT: user (34,9) said "MORE SHORT grass, NOT short_dry_grass".
    # Previously bumped short_dry_grass; that was my misread.  Bump
    # short_grass instead and drop short_dry_grass back near original.
    "KARST_BARRENS": [
        ("dead_bush", 0.02), ("short_dry_grass", 0.05), ("bush", 0.30),
        ("short_grass", 0.75), ("tall_dry_grass", 0.05),
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
    # S87: WAY way more vegetation per user (30,86 walk).  All entries bumped
    # significantly; moss_carpet bumped (wet substrate); added fern as another
    # mangrove-floor staple.
    "MANGROVE_COAST": [
        ("tall_grass", 0.32), ("sugar_cane", 0.22), ("short_grass", 0.35),
        ("bush", 0.10), ("short_dry_grass", 0.05), ("moss_carpet", 0.18),
        ("fern", 0.20), ("large_fern", 0.05),
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
    cliff_deg:      np.ndarray | None = None,
) -> None:
    """S64: place `snow[layers=1]` ground cover on snowy-biome pixels.
    Dithered at biome boundary via a distance-transform ramp — snow prob
    decays from 1.0 at interior to 0 at `ramp_blocks` outside the biome.
    Per-pixel coin decides placement within the ramp.  Does not overwrite
    existing ground_cover.  Skips water/ice/air surface blocks.

    S88: slope cap added.  When cliff_deg is provided, pixels with
    cliff_deg >= snow_carpet.slope_max_deg (default 35°) are excluded so
    snow does not stick to near-vertical faces -- physically realistic and
    keeps the Norterre cliff-rock aesthetic visible in snowy biomes.
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
    # S88 slope cap — no snow on steep faces.
    if cliff_deg is not None:
        slope_max = float(snow_cfg.get("slope_max_deg", 35.0))
        place_snow &= (cliff_deg < slope_max)
    if place_snow.any():
        ground_cover[place_snow] = "snow[layers=1]"


def _build_snow_line(biome_grid: np.ndarray, cfg: dict,
                     surface_y: np.ndarray | None = None,
                     north_factor: np.ndarray | None = None) -> np.ndarray:
    """Per-pixel snow-line altitude (MC-Y) from cfg.snow_lines. Snowy biomes get
    LOW lines (snow starts low); dry biomes HIGH lines (peaks only). Unlisted ->
    _default. With surface_y + convexity_coeff>0, the line GOES HIGHER on convex
    ground (wind-scoured ridges shed snow) and LOWER in concave ground (gullies/
    bowls collect it)."""
    sl = cfg.get("snow_lines", {}) if isinstance(cfg, dict) else {}
    out = np.full(biome_grid.shape, float(sl.get("_default", 485.0)), np.float32)
    for b, y in sl.items():
        if isinstance(b, str) and b.startswith("_"):
            continue
        out[biome_grid == b] = float(y)
    cc = float(sl.get("convexity_coeff", 0.0))
    if cc > 0.0 and surface_y is not None:
        from scipy.ndimage import gaussian_filter as _gf_sl
        syf = surface_y.astype(np.float32)
        conv = _gf_sl(syf, float(sl.get("convexity_sigma", 6.0))) - syf
        # conv > 0 = concave -> lower line (more snow); conv < 0 = convex -> higher.
        out = (out - cc * conv).astype(np.float32)
    # ASPECT: south-facing (sunny, north_factor->0) RAISES the line (snow melts);
    # north-facing (shaded, north_factor->1) LOWERS it (snow persists).
    ac = float(sl.get("aspect_coeff", 0.0))
    if ac > 0.0 and north_factor is not None:
        out = (out + ac * (0.5 - north_factor.astype(np.float32))).astype(np.float32)
    return out


def _apply_depth_snow(
    surface_blocks: np.ndarray,
    ground_cover:   np.ndarray,
    biome_grid:     np.ndarray,
    potential:      np.ndarray,      # (H,W) float32 [0,1] continuous snow potential
    gap_mask:       np.ndarray | None,
    cliff_deg:      np.ndarray | None,
    cfg:            dict,
    surface_y:      np.ndarray | None = None,  # (H,W) — for gully_only altitude gate
    tile_x:         int = 0,
    tile_y:         int = 0,
    north_factor:   np.ndarray | None = None,  # (H,W) — aspect for the snow line
) -> bool:
    """S89 depth-snow: paint snow DEPTH from the continuous physics potential.

    Replaces the flat snow_block + 95%-dappled snow[layers=1] carpet with a
    deterministic, potential-driven gradient that reads as real snowpack:

        P >= t_block         -> snow_block CAP (solid; high bowls + summits)
        t_layer <= P < t_block-> snow[layers=N] DRIFT  (N scales 1..max_layers)
        P <  t_layer          -> bare  (convex/steep/below-line rock shows)

    The snow_physics field already concentrates P in concave gullies (drifts
    finger down) and strips convex ridges + steep faces (slope_gate + curvature),
    so no dither coin is needed — the shape comes from terrain, not noise.

    Cold lowland biomes (carpet_biomes) below the alpine line keep a clean
    baseline snow[layers=1] so the Tundra-Valley / SBT-forest look survives.

    Returns True if it ran (so the caller skips the legacy carpet/flat-snow),
    False if disabled or the potential field is empty (legacy fallback).
    """
    dcfg = cfg.get("snow_physics", {}).get("depth", {})
    if not dcfg.get("enabled", False):
        return False
    if potential is None or float(potential.max()) <= 0.0:
        return False  # mask missing / not built -> legacy path

    t_layer   = float(dcfg.get("t_layer", 0.35))
    t_block   = float(dcfg.get("t_block", 0.62))
    max_layers= int(dcfg.get("max_layers", 7))
    slope_max = float(dcfg.get("slope_max_deg", 38.0))
    carpet_biomes = set(dcfg.get("carpet_biomes",
                                 ["SNOWY_BOREAL_TAIGA", "ARCTIC_TUNDRA",
                                  "FROZEN_FLATS"]))
    # Snow caps/drifts ONLY on snowy biomes (so a warm biome that happens to
    # reach alpine altitude does NOT get capped); within them snow covers
    # everything (rock, grass, ledges).
    snowy_biomes = set(dcfg.get("snowy_biomes",
                                ["SNOWY_BOREAL_TAIGA", "ARCTIC_TUNDRA",
                                 "FROZEN_FLATS", "BOREAL_ALPINE"]))

    P = potential.astype(np.float32)
    H, W = P.shape

    # Eligible surface: not water/ice/lava/air (snow needs solid ground).
    _NOT_SNOW_ON = ("water", "lava", "air", "ice", "packed_ice", "blue_ice")
    elig = np.ones((H, W), dtype=bool)
    for blk in _NOT_SNOW_ON:
        elig &= (surface_blocks != blk)
    # Steep faces stay bare (snow can't hold on near-vertical rock).
    if cliff_deg is not None:
        elig &= (cliff_deg < slope_max)
    # Restrict ALL snow placement to snowy biomes.
    if snowy_biomes:
        _snowy = np.zeros((H, W), dtype=bool)
        for b in snowy_biomes:
            _snowy |= (biome_grid == b)
        elig &= _snowy

    # ── GULLY-ONLY mode (S89 walk-3) ───────────────────────────────────
    # When the base snow is the Gaea mask (gap==7), depth-snow's job shrinks to
    # ONLY the high-gully fingers: solid snow_block in concave/sheltered gullies
    # at or above gully_min_y (the physics potential already pools there). No
    # drifts/carpet, no low-altitude snow. base_floor=0 in build_snow_physics
    # keeps the potential ~0 outside gullies/below the line.
    if bool(dcfg.get("gully_only", False)):
        if surface_y is not None:
            syf = surface_y.astype(np.float32)
            # FEATHERED, GULLY-BIASED floor anchored on the PER-BIOME snow line.
            # ridge cells (P just at threshold) snow at the biome's line; deep
            # gullies (high P from curvature/shelter) finger snow up to
            # gully_drop_blocks BELOW it. Per-pixel coin softens the edge into a
            # dithered, wandering snowline (no ruler-straight contour).
            ridge = _build_snow_line(biome_grid, cfg, surface_y, north_factor)
            drop = float(dcfg.get("gully_drop_blocks", 45.0))
            feather = max(1.0, float(dcfg.get("gully_feather_blocks", 10.0)))
            g = np.clip((P - t_block) / max(1e-3, 1.0 - t_block), 0.0, 1.0)
            floor_y = ridge - drop * g
            prob = np.clip((syf - floor_y) / feather + 0.5, 0.0, 1.0)
            _rng_g = np.random.default_rng(
                (tile_x * 2654435761 ^ tile_y * 40503 ^ 0x5A0FEA) & 0xFFFFFFFF)
            elig &= (_rng_g.random((H, W)).astype(np.float32) < prob)
        cap = elig & (P >= t_block)
        if cap.any():
            surface_blocks[cap] = "snow_block"
        return True

    # Cold-lowland carpet biomes get a clean baseline even where alpine
    # potential is ~0 (preserves Tundra Valley scattered-snow look).
    carpet = np.zeros((H, W), dtype=bool)
    for b in carpet_biomes:
        carpet |= (biome_grid == b)

    # ── CAP: solid snow_block where potential is high ──────────────────
    cap = elig & (P >= t_block)
    if cap.any():
        surface_blocks[cap] = "snow_block"

    # ── DRIFT: snow[layers=N] where potential is mid (N deepens with P) ─
    drift = elig & (P >= t_layer) & (P < t_block) & (ground_cover == "")
    if drift.any():
        denom = max(1e-3, t_block - t_layer)
        # N in 1..max_layers, deepening toward the cap.
        n = 1 + np.floor((P - t_layer) / denom * max_layers).astype(np.int32)
        n = np.clip(n, 1, max_layers)
        for lv in range(1, max_layers + 1):
            sel = drift & (n == lv)
            if sel.any():
                ground_cover[sel] = f"snow[layers={lv}]"

    # ── CARPET: clean layers=1 on cold lowland below the alpine line ────
    base = elig & carpet & (P < t_layer) & (ground_cover == "")
    if base.any():
        ground_cover[base] = "snow[layers=1]"

    return True


def _apply_grass_terraces(
    surface_blocks:    np.ndarray,
    subsurface_blocks: np.ndarray,
    ground_cover:      np.ndarray,
    surface_y:         np.ndarray,
    cliff_deg:         np.ndarray | None,
    rock_px:           np.ndarray,
    biome_grid:        np.ndarray,
    flow_tile:         np.ndarray | None,
    river_meta:        np.ndarray | None,
    snow_potential:    np.ndarray | None,
    cfg:               dict,
    tile_x:            int,
    tile_y:            int,
) -> None:
    """S89: grassy terraces/ledges on rocky slopes where soil realistically lands.

    A bench is a pixel that is LOCALLY FLAT (cliff_deg < bench_max_deg) yet
    SURROUNDED BY STEEP rock (neighbourhood max slope >= surround_min_deg) — i.e.
    a ledge on a cliff, not open meadow. Soil + seeds collect on these, plus the
    concave micro-catchments (positive curvature), so they green up while the
    steep faces between stay bare. Excludes active drainage (water cuts through),
    rivers, and arid/sand biomes. Coverage-capped + edge-dithered so it reads as
    organic patches, never a re-greened cliff. Runs AFTER the rock painters and
    BEFORE snow, so depth-snow overrides benches above the line (grassy bench
    dusted with snow, or solid cap higher up).
    """
    tcfg = cfg.get("grass_terraces", {})
    if not tcfg.get("enabled", False) or cliff_deg is None:
        return
    excl_biomes = set(tcfg.get("exclude_biomes", [
        "SAND_DUNE_DESERT", "SEMI_ARID_SHRUBLAND", "DESERT_STEPPE_TRANSITION",
        "DRY_WOODLAND_MAQUIS",
    ]))
    bench_max     = float(tcfg.get("bench_max_deg", 22.0))
    surround_min  = float(tcfg.get("surround_min_deg", 32.0))
    surround_rad  = int(tcfg.get("surround_radius_blocks", 4))
    conc_sigma    = float(tcfg.get("concavity_sigma", 3.0))
    conc_min      = float(tcfg.get("concavity_min", -0.3))  # >=this (blocks); <0 allows mild convex
    coverage      = float(tcfg.get("coverage", 0.6))
    rock_dilate   = int(tcfg.get("rock_dilate_blocks", 2))
    snow_handoff  = float(tcfg.get("snow_handoff_potential", 0.45))
    surf_blk      = tcfg.get("surface_block", "grass_block")
    sub_blk       = tcfg.get("subsurface_block", "dirt")
    gc_block      = tcfg.get("ground_cover", "short_grass")
    gc_prob       = float(tcfg.get("ground_cover_prob", 0.5))

    H, W = surface_y.shape
    from scipy.ndimage import maximum_filter, binary_dilation, gaussian_filter

    # locally flat, but on a cliff (neighbourhood is steep)
    flat = cliff_deg < bench_max
    nbr_steep = maximum_filter(cliff_deg.astype(np.float32),
                               size=2 * surround_rad + 1) >= surround_min
    bench = flat & nbr_steep
    # within / adjacent to the rock zone (it's a rock ledge)
    bench &= binary_dilation(rock_px, iterations=max(0, rock_dilate))
    if not bench.any():
        return

    # concavity: soil collects in concave catchments. conc>0 = basin.
    conc = gaussian_filter(surface_y.astype(np.float32), conc_sigma) \
        - surface_y.astype(np.float32)
    bench &= (conc >= conc_min)

    # exclude active drainage (water keeps the channel bare) + rivers
    if flow_tile is not None:
        _wash_min = float(cfg.get("washes", {}).get("min_flow", 0.003))
        bench &= (flow_tile < _wash_min)
    if river_meta is not None:
        bench &= (river_meta == 0)
    # hand off to snow above the line
    if snow_potential is not None:
        bench &= (snow_potential < snow_handoff)
    # arid/sand biomes don't green up
    for b in excl_biomes:
        bench &= (biome_grid != b)
    if not bench.any():
        return

    # S89 EDGE salt & pepper paint-out (~edge_fade_blocks): speckle the terrace
    # rim so it dissolves into the surrounding rock instead of a hard border.
    # Computed on the SOLID patch (before the coverage dither) so the distance
    # is meaningful. keep-prob ramps 0 at the edge -> 1 at fade_blocks inward.
    edge_fade = float(tcfg.get("edge_fade_blocks", 3.0))
    if edge_fade > 0.0 and bench.any():
        from scipy.ndimage import distance_transform_edt as _edt_t
        _edist = _edt_t(bench).astype(np.float32)   # dist to nearest non-bench
        _edge_keep = np.clip(_edist / edge_fade, 0.0, 1.0)
    else:
        _edge_keep = np.ones((H, W), dtype=np.float32)

    # coverage cap + organic dither (world-seeded) folded with the edge ramp
    rng = np.random.default_rng(
        (tile_x * 70207 ^ tile_y * 19937 ^ 0x6A455) & 0xFFFFFFFF)
    coin = rng.random((H, W)).astype(np.float32)
    bench &= (coin < (coverage * _edge_keep))
    if not bench.any():
        return

    surface_blocks[bench] = surf_blk
    subsurface_blocks[bench] = sub_blk
    if gc_block and gc_prob > 0.0:
        gc_sel = bench & (ground_cover == "") & (coin < coverage * gc_prob)
        if gc_sel.any():
            ground_cover[gc_sel] = gc_block


def _apply_strata_surface(
    surface_blocks:    np.ndarray,
    subsurface_blocks: np.ndarray,
    surface_y:         np.ndarray,
    biome_grid:        np.ndarray,
    lithology_tile:    np.ndarray | None,
    cliff_deg:         np.ndarray | None,
    river_meta:        np.ndarray | None,
    gap_mask:          np.ndarray | None,
    cfg:               dict,
    tile_x:            int,
    tile_y:            int,
    aspect:            np.ndarray | None = None,
) -> None:
    """S88 walk #4c: paint v2 STRATA blocks on surface_blk + sub_blk with a
    PROBABILISTIC FADE-IN over slope range [surface_min_deg, surface_fade_max_deg]
    (defaults 32° -> 35°).  Below surface_min_deg no strata.  Above
    surface_fade_max_deg strata is solid.  Between, per-cell coin against
    linear ramp.

    Uses the same 2-band mixed model + axis logic as chunk_writer's basement
    fill so cliff-top strata is consistent with the column below.

    Per group strata.axis:
      "Y_tilted" — band_idx from Y + waviness + col_y_noise + tilt (matches
                   chunk_writer's basement schedule -> seam-free continuity
                   from cliff top through column).
      "XZ_cols"  — band_idx from (x // col_size, z // col_size) hash;
                   surface and sub at same (x,z) share the band.

    Per band: pick primary OR secondary by coin against primary_pct.
    Speckle blocks form 1-4 block CLUSTERS following the strata axis:
      - Y_tilted: 1-4 block runs along tilt_dir_deg
      - XZ_cols:  1-4 block runs along Z (vertical-on-cliff streaks)
    Speckles paint surface_blk ONLY (no taller than y=1 per user spec).
    Veins NOT applied here (separate _apply_strata_veins_surface pass).

    Exclusions: river_meta>0 (water), gap==4 (floodplain), gap==7 (snow).
    Operates IN-PLACE on surface_blocks + subsurface_blocks.
    """
    # S89: superseded by _apply_rock_layers when rock_layers is enabled.
    if cfg.get("lithology", {}).get("rock_layers", {}).get("enabled", False):
        return
    if cliff_deg is None:
        return
    litho_cfg = cfg.get("lithology", {}) if isinstance(cfg, dict) else {}
    groups = litho_cfg.get("groups", {})
    if not groups:
        return
    H, W = surface_y.shape
    strata_global = litho_cfg.get("strata", {})
    surface_min_deg = float(strata_global.get("surface_min_deg", 32.0))
    surface_fade_max_deg = float(strata_global.get("surface_fade_max_deg", 35.0))

    steep_seed = cliff_deg >= surface_min_deg
    if not steep_seed.any():
        return
    # Per-cell fade-in probability over [surface_min_deg, surface_fade_max_deg]
    _fade_denom = max(0.1, surface_fade_max_deg - surface_min_deg)
    fade_prob = np.clip(
        (cliff_deg - surface_min_deg) / _fade_denom, 0.0, 1.0
    ).astype(np.float32)

    # Exclusion mask: water + floodplain + snow
    excl = np.zeros((H, W), dtype=bool)
    if river_meta is not None:
        excl |= (river_meta > 0)
    if gap_mask is not None:
        excl |= (gap_mask == 4)
        excl |= (gap_mask == 7)

    # Walk #6 (option C): SURFACE strata paint gated to rock_gap pixels only.
    # On steep no-rock-gap pixels we leave y (biome surface) + y-1 (dirt)
    # untouched; chunk_writer's basement strata fills y-2 and below with the
    # same band_a/band_b palette, giving the visual user described
    # (option C: "strata pushed down 2 blocks").  At band thicknesses 8-100
    # the 2-block Y offset of basement vs surface band id is imperceptible.
    if gap_mask is not None:
        rock_gap_mask = (gap_mask == 5)
        steep_seed = steep_seed & rock_gap_mask
        if not steep_seed.any():
            return

    if not (steep_seed & ~excl).any():
        return

    # Lithology resolved at full tile resolution
    if lithology_tile is not None and lithology_tile.shape != (H, W):
        from scipy.ndimage import zoom as _sd_zoom
        _zh = H / lithology_tile.shape[0]
        _zw = W / lithology_tile.shape[1]
        litho_at_res = _sd_zoom(lithology_tile, (_zh, _zw), order=0)
    else:
        litho_at_res = lithology_tile

    # Import helpers from chunk_writer so band schedule + waviness match
    # the basement fill exactly (cliff-top strata continues smoothly into
    # the column below).
    from core.chunk_writer import (
        _build_band_lut, _compute_xz_waviness, Y_RANGE,
    )

    tile_world_x = tile_x * W
    tile_world_z = tile_y * H
    _LUT_SIZE = Y_RANGE * 2
    col_world_x = (np.arange(W, dtype=np.float32) + tile_world_x)
    row_world_z = (np.arange(H, dtype=np.float32) + tile_world_z)

    def _idx_or_none(name: str) -> int | None:
        # block-name lookup is in the chunk_writer's palette; here we work
        # in string-name space (surface_blocks is an object array).
        # Just check the string is valid by returning the name itself
        # if non-empty.
        return name if (name and isinstance(name, str)) else None

    for gname, gdata in groups.items():
        strata = gdata.get("strata")
        if not strata:
            continue
        # NEW schema requires band_a + band_b
        band_a = strata.get("band_a")
        band_b = strata.get("band_b")
        if not band_a or not band_b:
            continue  # old-schema groups would have palette; skip if missing
        gid = int(gdata.get("id", 0))

        # Per-group mask: lithology.tif is source of truth.  No biome fallback.
        if litho_at_res is None:
            continue  # No painted lithology → no strata for this tile
        group_cols = (litho_at_res == gid)
        paint_mask_seed = steep_seed & group_cols & ~excl
        if not paint_mask_seed.any():
            continue
        # Per-group fade-in coin
        _fade_rng = np.random.default_rng(
            (tile_world_x * 9176381 ^ tile_world_z * 3741581 ^ gid * 0xFADE001) & 0xFFFFFFFF
        )
        _fade_coin = _fade_rng.random((H, W), dtype=np.float32)
        paint_mask = paint_mask_seed & (_fade_coin < fade_prob)
        if not paint_mask.any():
            continue

        axis = strata.get("axis", "Y_tilted")
        a_pri = _idx_or_none(band_a.get("primary"))
        a_sec = _idx_or_none(band_a.get("secondary"))
        a_pct = float(band_a.get("primary_pct", 50)) / 100.0
        b_pri = _idx_or_none(band_b.get("primary"))
        b_sec = _idx_or_none(band_b.get("secondary"))
        b_pct = float(band_b.get("primary_pct", 50)) / 100.0
        if a_pri is None or a_sec is None or b_pri is None or b_sec is None:
            continue  # invalid block names

        speckle_names = [s for s in strata.get("speckle_blocks", []) if s]
        speckle_rate = float(strata.get("speckle_rate", 0.0))

        # Compute band_idx_2d for both sy and sy-1
        # Y_tilted: based on surface_y per cell
        # XZ_cols:  based on (x // col_size, z // col_size) hash
        if axis == "XZ_cols":
            col_size = max(1, int(strata.get("col_size_blocks", 2)))
            col_mode = strata.get("col_hash_mode", "grid")  # walk #6: "diagonal" available
            salt_a = np.uint32(0xC4F12345 ^ ((gid * 1779033703) & 0xFFFFFFFF))
            salt_b = np.uint32(0x9E373B17 ^ ((gid * 2654435769) & 0xFFFFFFFF))
            if col_mode == "diagonal" and aspect is not None:
                # Walk #6 v2 + walk #7: ASPECT-PERPENDICULAR hash with smoothing
                # to fix per-pixel aspect noise.  along-cliff direction is
                # perpendicular to local aspect; hashing by distance along that
                # direction gives column stripes that ROTATE to align with the
                # local cliff face.
                #
                # Walk #7 fixes "noise" complaint:
                #   - col_size bumped to 16 (was 5) for visible wider stripes
                #   - aspect gaussian-smoothed at sigma=4 to kill pixel-level
                #     direction noise
                #   - terrain wobble dialed back via xz_wobble_amp config (0.25
                #     vs effective 1.0 in walk #6)
                _sg = cfg.get("lithology", {}).get("strata", {})
                _asp_sigma = float(_sg.get("aspect_smooth_sigma", 4.0))
                _wob_amp = float(_sg.get("xz_wobble_amp", 0.25))
                if _asp_sigma > 0.1:
                    from scipy.ndimage import gaussian_filter as _gf_asp
                    # Smooth sin + cos independently to avoid wrap-around issues
                    _cos_a_s = _gf_asp(np.cos(aspect).astype(np.float32), sigma=_asp_sigma)
                    _sin_a_s = _gf_asp(np.sin(aspect).astype(np.float32), sigma=_asp_sigma)
                else:
                    _cos_a_s = np.cos(aspect).astype(np.float32)
                    _sin_a_s = np.sin(aspect).astype(np.float32)
                _x_w = (np.arange(W, dtype=np.float32) + tile_world_x)[None, :]
                _z_w = (np.arange(H, dtype=np.float32) + tile_world_z)[:, None]
                _along = _x_w * _cos_a_s - _z_w * _sin_a_s  # (H, W) float32
                # Gentler wobble: surface_y * (xz_wobble_amp / col_size)
                _wobble = (
                    surface_y.astype(np.float32)
                    * (_wob_amp / max(1.0, float(col_size)))
                )
                _along_idx = ((_along + _wobble) // float(col_size)).astype(np.int64)
                # Hash that into bands
                _h = (_along_idx.astype(np.uint32) * salt_a) ^ salt_b
                _h ^= _h >> np.uint32(16)
                _h *= np.uint32(0x45D9F3B)
                _h ^= _h >> np.uint32(16)
                band_idx_sy = (_h & np.uint32(1)).astype(np.int8)
            else:  # original 2D grid hash (fallback when aspect missing)
                col_x_idx = ((np.arange(W, dtype=np.uint32) + tile_world_x) // col_size)
                col_z_idx = ((np.arange(H, dtype=np.uint32) + tile_world_z) // col_size)
                _h = (col_z_idx[:, None] * salt_a) ^ (col_x_idx[None, :] * salt_b)
                _h ^= _h >> np.uint32(16)
                _h *= np.uint32(0x45D9F3B)
                _h ^= _h >> np.uint32(16)
                band_idx_sy = (_h & np.uint32(1)).astype(np.int8)
            band_idx_sub = band_idx_sy  # XZ_cols: same band per column at any Y
        else:  # Y_tilted
            thickness_min = int(strata.get("thickness_min", 4))
            thickness_max = int(strata.get("thickness_max", 10))
            noise_amp = int(strata.get("noise_amp_blocks", 0))
            tilt_per100 = float(strata.get("tilt_per_100blocks", 0.0))
            tilt_dir_deg = float(strata.get("tilt_dir_deg", 0.0))

            lut_seed = tile_world_x * 73856093 ^ tile_world_z * 19349669 ^ gid * 2654435761
            band_lut = _build_band_lut(2, thickness_min, thickness_max, _LUT_SIZE, lut_seed)

            _xz_band_scale = max(8, thickness_max)
            base_waviness = _compute_xz_waviness(H, W, tile_world_x, tile_world_z, _xz_band_scale)
            if noise_amp > 0:
                ng_rng = np.random.default_rng(
                    (tile_world_x * 73856093 ^ tile_world_z * 19349669 ^ gid * 1234567) & 0xFFFFFFFF
                )
                col_y_noise = ng_rng.integers(-noise_amp, noise_amp + 1, size=(H, W), dtype=np.int32)
            else:
                col_y_noise = np.zeros((H, W), dtype=np.int32)
            tilt_rad = np.deg2rad(tilt_dir_deg)
            tilt_offset_2d = (
                (tilt_per100 / 100.0)
                * (col_world_x[None, :] * np.cos(tilt_rad)
                   + row_world_z[:, None] * np.sin(tilt_rad))
            ).astype(np.int32)
            y_eff_sy = (
                surface_y.astype(np.int32) + base_waviness + col_y_noise + tilt_offset_2d
            )
            y_eff_sub = y_eff_sy - 1
            band_idx_sy = band_lut[np.abs(y_eff_sy).astype(np.int32) % _LUT_SIZE]
            band_idx_sub = band_lut[np.abs(y_eff_sub).astype(np.int32) % _LUT_SIZE]

        # Per-cell primary/secondary pick (independent for sy and sy-1)
        primary_rng = np.random.default_rng(
            (tile_world_x * 1779033703 ^ tile_world_z * 0xDEADBEEF ^ gid * 0xCAFED00D) & 0xFFFFFFFF
        )
        coin_sy = primary_rng.random((H, W), dtype=np.float32)
        coin_sub = primary_rng.random((H, W), dtype=np.float32)
        # block names for sy
        primary_sy = np.where(band_idx_sy == 0, a_pri, b_pri)
        secondary_sy = np.where(band_idx_sy == 0, a_sec, b_sec)
        pct_sy = np.where(band_idx_sy == 0, a_pct, b_pct).astype(np.float32)
        block_sy = np.where(coin_sy < pct_sy, primary_sy, secondary_sy)
        # block names for sub
        primary_sub = np.where(band_idx_sub == 0, a_pri, b_pri)
        secondary_sub = np.where(band_idx_sub == 0, a_sec, b_sec)
        pct_sub = np.where(band_idx_sub == 0, a_pct, b_pct).astype(np.float32)
        block_sub = np.where(coin_sub < pct_sub, primary_sub, secondary_sub)

        # S88 walk #4c: SPECKLE = 1-4 block CLUSTERS following strata axis.
        # Y_tilted -> runs along tilt_dir_deg.  XZ_cols -> runs along Z
        # (vertical-on-cliff streaks).  Speckle paints SURFACE ONLY (no
        # taller than y=1 per user spec) so block_sub is untouched.
        if speckle_rate > 0 and speckle_names:
            sp_rng = np.random.default_rng(
                (tile_world_x * 83492791 ^ tile_world_z * 46508633 ^ gid * 1779033703) & 0xFFFFFFFF
            )
            seed_coin = sp_rng.random((H, W), dtype=np.float32)
            sp_seed = seed_coin < speckle_rate
            # Length picker: 0-3 additional blocks beyond the seed (so total
            # cluster length is 1..4).
            length_coin = sp_rng.random((H, W), dtype=np.float32)
            length_at_seed = np.clip(
                (length_coin * 4.0).astype(np.int32), 0, 3
            )
            length_at_seed = np.where(sp_seed, length_at_seed, 0)
            # Direction vector
            if axis == "XZ_cols":
                _dz, _dx = 1, 0
            else:
                _t_rad = np.deg2rad(float(strata.get("tilt_dir_deg", 0.0)))
                _dz = int(round(float(np.sin(_t_rad))))
                _dx = int(round(float(np.cos(_t_rad))))
                if _dz == 0 and _dx == 0:
                    _dx = 1
            # Per-seed block index (so a cluster shares one speckle block)
            if len(speckle_names) > 1:
                sp_pick = sp_rng.integers(
                    0, len(speckle_names), size=(H, W), dtype=np.int8
                )
            else:
                sp_pick = np.zeros((H, W), dtype=np.int8)
            # Build cluster mask: extend seed for L = 1..3 along (_dz, _dx)
            sp_mask = sp_seed.copy()
            # Track which speckle index covers each cluster pixel
            cluster_pick = np.where(sp_seed, sp_pick, np.int8(-1))
            for _L in range(1, 4):
                # Seeds whose length permits extension to >=L blocks beyond seed
                _seed_with = (length_at_seed >= _L) & sp_seed
                if not _seed_with.any():
                    continue
                _rolled = np.roll(_seed_with, shift=(_L * _dz, _L * _dx), axis=(0, 1))
                # Zero out wraparound rows/cols
                if _dz > 0:
                    _rolled[:_L * _dz, :] = False
                elif _dz < 0:
                    _rolled[_L * _dz:, :] = False
                if _dx > 0:
                    _rolled[:, :_L * _dx] = False
                elif _dx < 0:
                    _rolled[:, _L * _dx:] = False
                # Pick index propagation: rolled cells inherit seed's pick
                _rolled_pick = np.roll(sp_pick, shift=(_L * _dz, _L * _dx), axis=(0, 1))
                # Only update where not already part of cluster (preserve seed-of-record)
                _new = _rolled & ~sp_mask
                if _new.any():
                    cluster_pick = np.where(_new, _rolled_pick, cluster_pick)
                    sp_mask |= _new
            # Apply speckle to surface block ONLY
            if len(speckle_names) == 1:
                block_sy = np.where(sp_mask, speckle_names[0], block_sy)
            else:
                for _i, _spn in enumerate(speckle_names):
                    _m = sp_mask & (cluster_pick == _i)
                    if _m.any():
                        block_sy = np.where(_m, _spn, block_sy)

        # Write to surface_blocks + subsurface_blocks at paint_mask cells
        if paint_mask.any():
            surface_blocks[paint_mask] = block_sy[paint_mask]
            subsurface_blocks[paint_mask] = block_sub[paint_mask]


def _apply_strata_veins_surface(
    surface_blocks:    np.ndarray,
    subsurface_blocks: np.ndarray,
    surface_y:         np.ndarray,
    biome_grid:        np.ndarray,
    lithology_tile:    np.ndarray | None,
    cliff_deg:         np.ndarray | None,
    river_meta:        np.ndarray | None,
    gap_mask:          np.ndarray | None,
    cfg:               dict,
    tile_x:            int,
    tile_y:            int,
    vein_field_tile:   np.ndarray | None = None,
) -> None:
    """S88 walk #4b/4c: paint per-group vein blocks on surface_blk + sub_blk
    where the SAME vein_field used by chunk_writer's basement veins fires,
    gated by the strata fade-in over [surface_min_deg, surface_fade_max_deg]
    (32->35° default).

    Veins are mineral/rock seams cross-cutting strata along fault zones
    intersecting ridge lines.  Geologically they're VISIBLE on cliff
    faces, not just buried in the basement -- this function brings them
    to the surface so the user actually sees them.

    Multi-block vein_blocks list (1 or 2 blocks) -- per-cell uniform pick.
    Exclusions: river_meta>0, gap==4 (floodplain), gap==7 (snow).
    """
    if _overlay_off(cfg, "veins"):
        return
    if cliff_deg is None:
        return
    litho_cfg = cfg.get("lithology", {}) if isinstance(cfg, dict) else {}
    groups = litho_cfg.get("groups", {})
    if not groups:
        return
    H, W = surface_y.shape
    strata_global = litho_cfg.get("strata", {})
    surface_min_deg = float(strata_global.get("surface_min_deg", 32.0))
    surface_fade_max_deg = float(strata_global.get("surface_fade_max_deg", 35.0))

    steep_seed = cliff_deg >= surface_min_deg
    if not steep_seed.any():
        return
    _fade_denom = max(0.1, surface_fade_max_deg - surface_min_deg)
    fade_prob = np.clip(
        (cliff_deg - surface_min_deg) / _fade_denom, 0.0, 1.0
    ).astype(np.float32)

    # Walk #10/11: vein_field comes from PRECOMPUTE MASK (vein_field.tif).
    # Fall back to runtime computation if mask not provided.
    if vein_field_tile is not None:
        # tile_streamer normalises uint8 -> float32 [0,1]; convert back to byte
        vein_intensity_byte = (vein_field_tile * 255.0).astype(np.int32)
        vein_mask_thr = int(strata_global.get("vein_mask_threshold", 32))
        vein_field = vein_intensity_byte >= vein_mask_thr
    else:
        # Legacy runtime path (kept for fallback if mask missing)
        from core.chunk_writer import _compute_vein_field
        vein_lap_thr = float(strata_global.get("vein_lap_threshold", 4.0))
        vein_fault_scale = int(strata_global.get("vein_fault_scale_blocks", 80))
        vein_fault_width = float(strata_global.get("vein_fault_width", 0.08))
        tile_world_x = tile_x * W
        tile_world_z = tile_y * H
        vein_field = _compute_vein_field(
            surface_y, tile_world_x, tile_world_z,
            lap_threshold=vein_lap_thr,
            fault_scale_blocks=vein_fault_scale,
            fault_width=vein_fault_width,
        )
    if not vein_field.any():
        return

    excl = np.zeros((H, W), dtype=bool)
    if river_meta is not None:
        excl |= (river_meta > 0)
    if gap_mask is not None:
        excl |= (gap_mask == 4)
        excl |= (gap_mask == 7)

    # Walk #10.1: veins ONLY fire on rock_gap pixels (per user — was covering
    # everything when slope-only-gated).
    if gap_mask is not None:
        _rock_gate = (gap_mask == 5)
        vein_field = vein_field & _rock_gate
        if not vein_field.any():
            return
        del _rock_gate

    if lithology_tile is not None and lithology_tile.shape != (H, W):
        from scipy.ndimage import zoom as _sd_zoom_v
        _zh = H / lithology_tile.shape[0]
        _zw = W / lithology_tile.shape[1]
        litho_at_res = _sd_zoom_v(lithology_tile, (_zh, _zw), order=0)
    else:
        litho_at_res = lithology_tile

    for gname, gdata in groups.items():
        strata = gdata.get("strata")
        if not strata:
            continue
        vein_blocks = strata.get("vein_blocks", [])
        vein_amp = float(strata.get("vein_amp", 0.0))
        if not vein_blocks or vein_amp <= 0:
            continue
        gid = int(gdata.get("id", 0))

        # Per-group mask: lithology.tif is source of truth.  No biome fallback.
        if litho_at_res is None:
            continue
        group_cols = (litho_at_res == gid)

        # Walk #4c: fade-in gate (32-35°) before vein_amp coin
        _vfade_rng = np.random.default_rng(
            (tile_x * 9176381 ^ tile_y * 3741581 ^ gid * 0xFADEBEEF) & 0xFFFFFFFF
        )
        _vfade_coin = _vfade_rng.random((H, W), dtype=np.float32)
        candidate = (
            steep_seed & group_cols & ~excl & vein_field & (_vfade_coin < fade_prob)
        )
        if not candidate.any():
            continue

        v_rng = np.random.default_rng(
            (tile_x * 53248917 ^ tile_y * 67280421 ^ gid * 0xBEEFCAFE) & 0xFFFFFFFF
        )
        coin = v_rng.random((H, W), dtype=np.float32)
        v_seed = candidate & (coin < vein_amp)
        if not v_seed.any():
            continue

        # Walk #7: VEIN STREAKING.  Each seed pixel is extended into a
        # 3-8 block run along the strata axis so veins read as visible
        # streaks instead of isolated single-block scatter.
        _streak_min = int(strata_global.get("vein_streak_min", 3))
        _streak_max = int(strata_global.get("vein_streak_max", 8))
        # Walk #10.2: CROSS-CUTTING orientation — veins run PERPENDICULAR to
        # strata axis (real mineral veins/dikes cut ACROSS bedding, not along
        # it).  This is the iconic "quartz vein through schist" visual.
        axis = strata.get("axis", "Y_tilted")
        if axis == "XZ_cols":
            # XZ_cols strata = vertical columns (along Z).  Perpendicular = X axis.
            _dz, _dx = 0, 1
        else:
            # Y_tilted strata bands run perpendicular to tilt_dir_deg.
            # Cross-cutting = ALONG tilt_dir_deg + 90° offset.
            _t_rad = np.deg2rad(float(strata.get("tilt_dir_deg", 0.0)) + 90.0)
            _dz = int(round(float(np.sin(_t_rad))))
            _dx = int(round(float(np.cos(_t_rad))))
            if _dz == 0 and _dx == 0:
                _dz = 1  # vertical fallback (more vein-like than horizontal)
        # Per-seed streak length picker
        _len_coin = v_rng.random((H, W), dtype=np.float32)
        _len_range = max(1, _streak_max - _streak_min + 1)
        _length_at_seed = np.clip(
            (_len_coin * float(_len_range)).astype(np.int32) + _streak_min,
            _streak_min, _streak_max,
        )
        _length_at_seed = np.where(v_seed, _length_at_seed, 0)
        # Per-seed block index (so a streak shares one vein block)
        if len(vein_blocks) > 1:
            _v_pick = v_rng.integers(0, len(vein_blocks), size=(H, W), dtype=np.int8)
        else:
            _v_pick = np.zeros((H, W), dtype=np.int8)
        # Build streak mask: extend seeds along (dz,dx) for L blocks
        v_mask = v_seed.copy()
        streak_pick = np.where(v_seed, _v_pick, np.int8(-1))
        for _L in range(1, _streak_max + 1):
            _seed_with = (_length_at_seed > _L) & v_seed
            if not _seed_with.any():
                break
            _rolled = np.roll(_seed_with, shift=(_L * _dz, _L * _dx), axis=(0, 1))
            if _dz > 0:
                _rolled[:_L * _dz, :] = False
            elif _dz < 0:
                _rolled[_L * _dz:, :] = False
            if _dx > 0:
                _rolled[:, :_L * _dx] = False
            elif _dx < 0:
                _rolled[:, _L * _dx:] = False
            _rolled_pick = np.roll(_v_pick, shift=(_L * _dz, _L * _dx), axis=(0, 1))
            _new = _rolled & ~v_mask
            if _new.any():
                streak_pick = np.where(_new, _rolled_pick, streak_pick)
                v_mask |= _new
        # Re-apply candidate constraints to the extended streaks (don't
        # paint veins through water, snow, non-rock-gap if gated, etc).
        v_mask &= candidate

        # Walk #10.2: HALO zone — paint 1-block ring around veins using the
        # group's varnish_palette as "host rock chemically altered by the
        # vein fluids".  Real veins have alteration halos extending a few
        # cm around the main fracture; this is the MC-scale analog.
        if v_mask.any():
            from scipy.ndimage import binary_dilation as _bd_halo
            _halo = _bd_halo(v_mask, iterations=1) & ~v_mask & candidate
            if _halo.any():
                _halo_pal = gdata.get("varnish_palette") or vein_blocks
                _halo_arr = np.asarray(_halo_pal, dtype=object)
                _n_halo = int(_halo.sum())
                surface_blocks[_halo] = _halo_arr[
                    v_rng.integers(0, len(_halo_pal), size=_n_halo)
                ]
                # Halo is surface-only; don't paint subsurface (matches
                # varnish convention — 1 block thick stain).

        # Paint per-cell streak block (core veins overwrite halo where they overlap)
        if len(vein_blocks) == 1:
            surface_blocks[v_mask] = vein_blocks[0]
            subsurface_blocks[v_mask] = vein_blocks[0]
        else:
            for i, vb in enumerate(vein_blocks):
                m = v_mask & (streak_pick == i)
                if m.any():
                    surface_blocks[m] = vb
                    subsurface_blocks[m] = vb


def _apply_concavity_drainage(
    surface_blocks:    np.ndarray,
    subsurface_blocks: np.ndarray,
    surface_y:         np.ndarray,
    lithology_tile:    np.ndarray | None,
    river_meta:        np.ndarray | None,
    gap_mask:          np.ndarray | None,
    cfg:               dict,
    tile_x:            int,
    tile_y:            int,
    cliff_deg:         np.ndarray | None = None,
) -> None:
    """S88 walk #6 NEW pass: paint bedrock_drainage_palette on concave terrain.

    Detects pixels where the surface_y Laplacian is strongly positive (a
    local depression / bowl).  Complements the existing bedrock_drainage
    MASK painter (which uses flow accumulation): this catches gully
    pinch-points, depression bowls, and any negative-curvature feature
    the flow mask missed.

    Per-pixel lithology via lithology_tile -> group bedrock_drainage_palette.
    Excludes water, floodplain, snow.
    """
    if _overlay_off(cfg, "concavity"):
        return
    cc = cfg.get("lithology", {}).get("concavity", {}) if isinstance(cfg, dict) else {}
    if not cc.get("enabled", False):
        return
    lap_threshold = float(cc.get("lap_threshold", 3.0))
    palette_key = cc.get("palette_key", "concavity_palette")  # walk #9: new key
    dilate_blocks = int(cc.get("dilate_blocks", 0))
    slope_min_deg = float(cc.get("slope_min_deg", 0.0))  # walk #9.1: slope gate

    H, W = surface_y.shape
    sy = surface_y.astype(np.float32)
    # 3x3 discrete Laplacian. Positive = local depression (neighbors higher).
    lap = np.zeros_like(sy)
    lap[1:-1, 1:-1] = (
        sy[ :-2, 1:-1] + sy[2:  , 1:-1] +
        sy[1:-1,  :-2] + sy[1:-1, 2:  ] -
        4.0 * sy[1:-1, 1:-1]
    )
    paint_mask = lap >= lap_threshold

    # Walk #9.1: slope gate (matches strata fade-in floor at 32° default)
    if slope_min_deg > 0 and cliff_deg is not None:
        paint_mask &= (cliff_deg >= slope_min_deg)

    # Walk #9: dilate detected pixels into small patches for visibility
    if dilate_blocks > 0 and paint_mask.any():
        from scipy.ndimage import binary_dilation as _bd_conv
        paint_mask = _bd_conv(paint_mask, iterations=dilate_blocks)
        # Re-apply slope gate post-dilation so we don't bleed onto flat
        if slope_min_deg > 0 and cliff_deg is not None:
            paint_mask &= (cliff_deg >= slope_min_deg)

    excl = np.zeros((H, W), dtype=bool)
    if river_meta is not None:
        excl |= (river_meta > 0)
    if gap_mask is not None:
        excl |= (gap_mask == 4) | (gap_mask == 7)
    paint_mask &= ~excl
    if not paint_mask.any():
        return

    # Resolve lithology at native res
    if lithology_tile is None:
        return  # no painted lithology, no per-group palette
    if lithology_tile.shape != (H, W):
        from scipy.ndimage import zoom as _z
        litho_at_res = _z(
            lithology_tile,
            (H / lithology_tile.shape[0], W / lithology_tile.shape[1]),
            order=0,
        )
    else:
        litho_at_res = lithology_tile

    groups = cfg.get("lithology", {}).get("groups", {})
    gid_to_pal: dict[int, list] = {}
    DEFAULT = ["andesite", "stone", "gravel"]
    for _gname, _gdata in groups.items():
        _gid = int(_gdata.get("id", 0))
        gid_to_pal[_gid] = _gdata.get(palette_key) or DEFAULT

    rng = np.random.default_rng(
        (tile_x * 0xC0FFEE ^ tile_y * 0xDEAD ^ 0xBE) & 0xFFFFFFFF
    )
    for _gid_u in np.unique(litho_at_res[paint_mask]):
        _gid = int(_gid_u)
        _bm = paint_mask & (litho_at_res == _gid)
        if not _bm.any():
            continue
        _pal = gid_to_pal.get(_gid, DEFAULT)
        _arr = np.asarray(_pal, dtype=object)
        _n = int(_bm.sum())
        surface_blocks[_bm] = _arr[rng.integers(0, len(_pal), size=_n)]
        subsurface_blocks[_bm] = _arr[rng.integers(0, len(_pal), size=_n)]


def _apply_basaltic_joints(
    surface_blocks:    np.ndarray,
    subsurface_blocks: np.ndarray,
    lithology_tile:    np.ndarray | None,
    gap_mask:          np.ndarray | None,
    joint_pattern_tile: np.ndarray | None,
    cfg:               dict,
    tile_x:            int,
    tile_y:            int,
) -> None:
    """S88 walk #11: paint VISIBLE COLUMNAR JOINTS on basaltic groups.

    Real basalt cools into hexagonal column pillars with visible fracture
    planes between columns.  We've had XZ_cols hash for years giving
    columns the right band-per-column behavior — but ADJACENT COLUMNS
    look identical because the band swap is subtle.

    This pass paints the JOINT BOUNDARIES with a darker block, producing
    visible vertical fracture lines on cliff faces between columns.

    Gated to: gap_mask == 5 (rock_gap pixels) AND lithology in basaltic
    groups (gid 2 = arid_basaltic, gid 3 = temperate_basaltic).
    """
    # S89: superseded by _apply_rock_layers when rock_layers is enabled.
    if cfg.get("lithology", {}).get("rock_layers", {}).get("enabled", False):
        return
    if gap_mask is None or lithology_tile is None or joint_pattern_tile is None:
        return
    rock_zone = (gap_mask == 5)
    if not rock_zone.any():
        return
    H, W = rock_zone.shape

    # Resolve lithology
    if lithology_tile.shape != (H, W):
        from scipy.ndimage import zoom as _zj
        litho_at_res = _zj(
            lithology_tile,
            (H / lithology_tile.shape[0], W / lithology_tile.shape[1]),
            order=0,
        )
    else:
        litho_at_res = lithology_tile

    # Find basaltic gids from config (groups with axis == XZ_cols)
    groups = cfg.get("lithology", {}).get("groups", {})
    basaltic_gids = set()
    gid_to_joint_block: dict[int, str] = {}
    for _gname, _gdata in groups.items():
        if _gdata.get("strata", {}).get("axis") == "XZ_cols":
            _gid = int(_gdata.get("id", 0))
            basaltic_gids.add(_gid)
            # Joint block = the DARKER of the two band primaries (basalt > smooth_basalt)
            _ba = _gdata.get("strata", {}).get("band_a", {})
            gid_to_joint_block[_gid] = _ba.get("primary", "basalt")
    if not basaltic_gids:
        return

    basaltic_mask = np.zeros((H, W), dtype=bool)
    for _gid in basaltic_gids:
        basaltic_mask |= (litho_at_res == _gid)
    if not basaltic_mask.any():
        return

    # Joint threshold: byte >= 128 = boundary pixel
    _intensity_byte = (joint_pattern_tile * 255.0).astype(np.int32)
    joint_zone = rock_zone & basaltic_mask & (_intensity_byte >= 128)
    if not joint_zone.any():
        return

    # Paint joint pixels per-litho with the group's joint block
    for _gid in basaltic_gids:
        _bm = joint_zone & (litho_at_res == _gid)
        if not _bm.any():
            continue
        _blk = gid_to_joint_block.get(_gid, "basalt")
        surface_blocks[_bm] = _blk
        subsurface_blocks[_bm] = _blk


def _overlay_off(cfg, name):
    """S89-v6: True if the legacy rock overlay `name` should be SKIPPED.

    When rock_layers is enabled, the legacy rock overlays (veins / wash /
    varnish / bedrock_drainage / concavity) only run if explicitly turned on in
    lithology.rock_layers.overlays. Default OFF so the new system shows bare
    (rock_layers + talus + cliff_cap). When rock_layers is disabled, returns
    False so the legacy path is unchanged."""
    rl = cfg.get("lithology", {}).get("rock_layers", {}) if isinstance(cfg, dict) else {}
    if not rl.get("enabled", False):
        return False
    return not rl.get("overlays", {}).get(name, False)


def _paint_solid_dither(surface_blocks, subsurface_blocks, mask, blocks, coin,
                        dither, paint_sub=True):
    """S89-v3: paint a layer SOLID with the first block, sprinkling the
    remaining block(s) only on a thin `dither` fraction (~0.03-0.05) so each
    layer reads as a solid band lightly flecked, not a 50/50 salt-and-pepper
    static. blocks[0] dominates (1-dither of the mask); blocks[1:] share the
    [0, dither) band of `coin`."""
    if not mask.any() or not blocks:
        return
    if len(blocks) == 1:
        surface_blocks[mask] = blocks[0]
        if paint_sub:
            subsurface_blocks[mask] = blocks[0]
        return
    dom = mask & (coin >= dither)
    surface_blocks[dom] = blocks[0]
    if paint_sub:
        subsurface_blocks[dom] = blocks[0]
    rest = blocks[1:]
    nb = len(rest)
    for j, blk in enumerate(rest):
        lo = dither * j / nb
        hi = dither * (j + 1) / nb
        bm = mask & (coin >= lo) & (coin < hi)
        if bm.any():
            surface_blocks[bm] = blk
            if paint_sub:
                subsurface_blocks[bm] = blk


def _apply_rock_layers(
    surface_blocks:    np.ndarray,
    subsurface_blocks: np.ndarray,
    lithology_tile:    np.ndarray | None,
    rock_layers_tile:  np.ndarray | None,
    cfg:               dict,
    tile_x:            int,
    tile_y:            int,
    talus_apron_tile:  np.ndarray | None = None,
) -> None:
    """S89: paint rock_layers tiers (dark / mid / light) per lithology group.

    Replaces the legacy rock_gap-palette + strata + basaltic-joints surface
    passes when cfg.lithology.rock_layers.enabled.  `rock_layers_tile` is the
    baked tier mask (0=not rock, 1=dark, 2=mid, 3=light) delivered normalised
    [0,1] (uint8/255) -> recover the integer tier via round(*255).  Each tier
    paints its 2 textures via per-pixel salt-and-pepper white noise on BOTH
    surface and subsurface (coherent column).  This is the BASE rock paint;
    veins / wash / talus / varnish / cliff_cap overlay it downstream.
    """
    rl_cfg = cfg.get("lithology", {}).get("rock_layers", {}) if isinstance(cfg, dict) else {}
    if not rl_cfg.get("enabled", False):
        return
    if rock_layers_tile is None or lithology_tile is None:
        return
    groups_cfg = rl_cfg.get("groups", {})
    if not groups_cfg:
        return
    H, W = surface_blocks.shape
    tier = np.round(rock_layers_tile * 255.0).astype(np.int32)
    if not (tier >= 1).any():
        return

    # TALUS mask (computed once; used to keep talus clean of rock fade below).
    # The talus apron has its own fine/coarse palette and must NOT receive rock
    # fade bleed (edge stroke OR per-tier edge fade) -- that's the "extra fade
    # zone on talus" the user flagged.
    _talus_m = None
    if talus_apron_tile is not None:
        _ta = talus_apron_tile
        if _ta.shape != (H, W):
            from scipy.ndimage import zoom as _z_ta
            _ta = _z_ta(_ta, (H / _ta.shape[0], W / _ta.shape[1]), order=0)
        _talus_m = _ta > 0.0

    # S89 walk: EDGE STROKE on the rock/land boundary (ORIGINAL behaviour —
    # boundary at the rock edge). Roughen the rock margin over a ~4-5 block ring:
    # grow dark-tier specks OUT onto adjacent land, bite land specks INTO the
    # rock edge; amp fades to 0 at the ring edge. EXCEPTION: never grow onto
    # talus cells, so the apron stays clean.
    _estroke = int(rl_cfg.get("edge_stroke_blocks", 4))
    _estroke_amp = float(rl_cfg.get("edge_stroke_amp", 0.40))
    if _estroke >= 1 and _estroke_amp > 0.0:
        _rock = tier >= 1
        if _rock.any() and not _rock.all():
            from scipy.ndimage import distance_transform_edt as _edt_rs
            _din = _edt_rs(_rock).astype(np.float32)
            _dout = _edt_rs(~_rock).astype(np.float32)
            _ed = np.where(_rock, _din, _dout)          # ~1 at the rock/land boundary
            _ring = _ed <= _estroke
            if _ring.any():
                _amp = (_estroke_amp
                        * np.clip(1.0 - (_ed - 1.0) / _estroke, 0.0, 1.0))
                _ers = np.random.default_rng(
                    (tile_x * 48271 ^ tile_y * 31337 ^ 0x52C3) & 0xFFFFFFFF)
                _ec = _ers.random((H, W), dtype=np.float32)
                _bite = _rock & _ring & (_ec < _amp)
                _grow = (~_rock) & _ring & (_ec < _amp)
                if _talus_m is not None:                # keep talus clean
                    _grow &= ~_talus_m
                tier[_bite] = 0
                tier[_grow] = 1
                del _din, _dout, _ed, _ring, _amp, _ers, _ec, _bite, _grow
            del _rock

    if lithology_tile.shape != (H, W):
        from scipy.ndimage import zoom as _z_rl
        litho = _z_rl(
            lithology_tile,
            (H / lithology_tile.shape[0], W / lithology_tile.shape[1]),
            order=0,
        )
    else:
        litho = lithology_tile

    name_to_id = {
        _gn: int(_gd.get("id", 0))
        for _gn, _gd in cfg.get("lithology", {}).get("groups", {}).items()
    }
    rng = np.random.default_rng(
        (tile_x * 73856093 ^ tile_y * 19349663 ^ 0x52CC15) & 0xFFFFFFFF
    )
    coin = rng.random((H, W), dtype=np.float32)
    layer_dither = float(rl_cfg.get("layer_dither", 0.04))
    # S89: 2-3 block fade-OUT per tier. Each tier bleeds `edge_fade_blocks`
    # downslope (into LOWER tiers + tier-0 land of the SAME lithology) with a
    # linear-fade coin, so every rock edge feathers instead of being a hard
    # slope contour. Painted in tier order (dark->mid->light) so a higher
    # tier's solid core overwrites a lower tier's bleed -> crisp where it
    # matters, soft at the outer rock/land boundary. Talus is exempt (own bounds).
    _edge_fade = int(rl_cfg.get("edge_fade_blocks", 0))
    _fade_coin = None
    if _edge_fade > 0:
        from scipy.ndimage import distance_transform_edt as _edt_rl
        _fr = np.random.default_rng(
            (tile_x * 2654435761 ^ tile_y * 40503 ^ 0xED6EFADE) & 0xFFFFFFFF)
        _fade_coin = _fr.random((H, W), dtype=np.float32)
    TIERS = ((1, "dark"), (2, "mid"), (3, "light"))
    for gname, gdata in groups_cfg.items():
        gid = name_to_id.get(gname)
        if gid is None:
            continue
        gmask = (litho == gid)
        if not gmask.any():
            continue
        for tval, key in TIERS:
            pal = gdata.get(key)
            if not pal:
                continue
            core = gmask & (tier == tval)
            if not core.any():
                continue
            m = core
            if _edge_fade > 0:
                # distance from this tier's core, outward
                _dist = _edt_rl(~core).astype(np.float32)
                _ring = gmask & (tier < tval) & (_dist > 0) & (_dist <= _edge_fade)
                if _talus_m is not None:        # talus exempt: no rock fade bleed
                    _ring = _ring & ~_talus_m
                if _ring.any():
                    _fade = np.clip(1.0 - _dist / float(_edge_fade), 0.0, 1.0)
                    m = core | (_ring & (_fade_coin < _fade))
            _paint_solid_dither(surface_blocks, subsurface_blocks, m, pal,
                                coin, layer_dither, paint_sub=True)


def _apply_talus(
    surface_blocks:    np.ndarray,
    subsurface_blocks: np.ndarray,
    lithology_tile:    np.ndarray | None,
    talus_apron_tile:  np.ndarray | None,
    cfg:               dict,
    tile_x:            int,
    tile_y:            int,
    river_meta:        np.ndarray | None = None,
    gap_mask:          np.ndarray | None = None,
) -> None:
    """S89: paint the debris apron with GRAIN SORTING + DEPTH.

    Rides with the new rock system (gated on lithology.rock_layers.enabled);
    replaces the legacy generic talus painter.  `talus_apron_tile` is the
    continuous run-out intensity in [0,1] (high at the cliff base, decaying to
    the toe).  Effects, all derived from that single field:
      - taper:  coverage probability ∝ intensity (sparse, scattered at the toe)
      - grain:  P(fine) = intensity -> fine dust near the base (high intensity),
                coarse clasts at the toe (low intensity); dithered boundary.
      - depth:  where intensity >= depth_intensity, also paint the subsurface
                block (thick apron at the base vs 1-block veneer at the toe).
    Per-group fine/coarse palettes from cfg.lithology.talus.groups.  (Full
    multi-block depth would need chunk_writer column support — deferred.)
    """
    litho_cfg = cfg.get("lithology", {}) if isinstance(cfg, dict) else {}
    _rl = litho_cfg.get("rock_layers", {})
    if not _rl.get("enabled", False):
        return
    # S89: talus toggle (default ON). Set rock_layers.overlays.talus=false to
    # disable the apron for a diagnostic render.
    if not _rl.get("overlays", {}).get("talus", True):
        return
    if talus_apron_tile is None or lithology_tile is None:
        return
    t_cfg = litho_cfg.get("talus", {})
    groups_cfg = t_cfg.get("groups", {})
    if not groups_cfg:
        return
    H, W = surface_blocks.shape
    inten = np.clip(talus_apron_tile.astype(np.float32), 0.0, 1.0)
    if not (inten > 0).any():
        return
    floor = float(t_cfg.get("coverage_floor", 0.10))
    depth_at = float(t_cfg.get("depth_intensity", 0.55))

    excl = np.zeros((H, W), dtype=bool)
    if river_meta is not None:
        excl |= (river_meta > 0)
    if gap_mask is not None:
        excl |= (gap_mask == 4) | (gap_mask == 7)

    if lithology_tile.shape != (H, W):
        from scipy.ndimage import zoom as _z_t
        litho = _z_t(
            lithology_tile,
            (H / lithology_tile.shape[0], W / lithology_tile.shape[1]),
            order=0,
        )
    else:
        litho = lithology_tile

    name_to_id = {
        _gn: int(_gd.get("id", 0))
        for _gn, _gd in litho_cfg.get("groups", {}).items()
    }
    rng = np.random.default_rng(
        (tile_x * 40503 ^ tile_y * 20011 ^ 0x7A1115) & 0xFFFFFFFF
    )
    cov_coin = rng.random((H, W), dtype=np.float32)
    grain_coin = rng.random((H, W), dtype=np.float32)
    tex_coin = rng.random((H, W), dtype=np.float32)  # within-band solid+dither
    layer_dither = float(t_cfg.get("layer_dither", 0.04))
    grain_cut = float(t_cfg.get("grain_cut", 0.5))
    grain_dither = float(t_cfg.get("grain_dither", 0.06))
    edge_band = float(t_cfg.get("edge_band", 0.10))

    # Coverage: SOLID (100%) at/above fade_hi, then a TIGHT STEPPED toe fade
    # 100 -> 75 -> 50 -> 25 -> 0 over the compact [fade_lo, fade_hi] intensity
    # window (S89: was a single linear 100->0 ramp over a wide band, which read
    # as an unnatural slow fade). Smaller window = tighter footprint; the 4
    # discrete steps read as a natural scree taper.
    _fade_lo = float(t_cfg.get("fade_lo", floor))
    _fade_hi = float(t_cfg.get("fade_hi", floor + edge_band))
    _t = np.clip((inten - _fade_lo) / max(1e-3, _fade_hi - _fade_lo), 0.0, 1.0)
    _steps = max(1, int(t_cfg.get("coverage_steps", 4)))  # higher = smoother/more consistent gradient
    _cov = np.ceil(_t * _steps) / _steps
    _cov = np.where(_t >= 1.0, 1.0, _cov)
    base_zone = (_cov > 0.0) & (cov_coin < _cov) & ~excl
    if not base_zone.any():
        return
    # Grain STAGGERED by intensity (a clean band, lightly dithered at the cut):
    # coarse near the cliff base (high intensity) wears down to fine at the toe.
    _cut = grain_cut + (grain_coin - 0.5) * (2.0 * grain_dither)
    is_coarse = inten >= _cut
    deep = inten >= depth_at

    for gname, gdata in groups_cfg.items():
        gid = name_to_id.get(gname)
        if gid is None:
            continue
        gmask = (litho == gid) & base_zone
        if not gmask.any():
            continue
        fine = gdata.get("fine") or []
        coarse = gdata.get("coarse") or []
        if not fine or not coarse:
            continue
        for blocks, band in ((coarse, gmask & is_coarse), (fine, gmask & ~is_coarse)):
            if not band.any():
                continue
            # surface: solid band + thin dither of the 2nd texture
            _paint_solid_dither(surface_blocks, subsurface_blocks, band,
                                blocks, tex_coin, layer_dither, paint_sub=False)
            # depth: subsurface only where the apron is thick (near the base)
            _dz = band & deep
            if _dz.any():
                _paint_solid_dither(surface_blocks, subsurface_blocks, _dz,
                                    blocks, tex_coin, layer_dither, paint_sub=True)


def _apply_cliff_cap(
    surface_blocks:    np.ndarray,
    subsurface_blocks: np.ndarray,
    lithology_tile:    np.ndarray | None,
    cliff_cap_tile:    np.ndarray | None,
    cfg:               dict,
    tile_x:            int,
    tile_y:            int,
    river_meta:        np.ndarray | None = None,
    gap_mask:          np.ndarray | None = None,
    rock_layers_tile:  np.ndarray | None = None,
) -> None:
    """S89: paint the scoured convex-peak cap (per-group palette, salt-and-pepper,
    surface + subsurface).  Rides with lithology.rock_layers.enabled; replaces the
    generic cap painter.  `cliff_cap_tile` is the convexity-exposure intensity
    [0,1].  Tree + ground-cover suppression are handled separately (the GC-kill
    block below in decorate_surface + schematic_placement)."""
    litho_cfg = cfg.get("lithology", {}) if isinstance(cfg, dict) else {}
    if not litho_cfg.get("rock_layers", {}).get("enabled", False):
        return
    if cliff_cap_tile is None or lithology_tile is None:
        return
    cc = litho_cfg.get("cliff_cap", {})
    groups_cfg = cc.get("groups", {})
    if not groups_cfg:
        return
    H, W = surface_blocks.shape
    thr = int(cc.get("intensity_threshold", 8))
    # S89 walk3: cap noise pass removed (was cap_dither threshold jitter) — it
    # only raggedized the cap/snow boundary; the cap follows the clean convexity
    # contour now.
    inten = (cliff_cap_tile * 255.0).astype(np.float32)
    zone = inten >= thr
    if river_meta is not None:
        zone &= (river_meta <= 0)
    if gap_mask is not None:
        zone &= (gap_mask != 7)  # snow wins on the crest
    # S89: HIGH SLOPE PAINTS OVER CAPS. Cap is for the gentle convex crown;
    # where a peak's flank is steep (rock tier >= 2 = mid/light) the rock
    # ladder wins, so exclude those pixels from the cap zone entirely.
    _tier_cc = None
    if rock_layers_tile is not None:
        _tier_cc = np.round(rock_layers_tile * 255.0).astype(np.int32)
        zone &= (_tier_cc < 2)
    if not zone.any():
        return
    dil = int(cc.get("dilate_blocks", 0))
    if dil > 0:
        from scipy.ndimage import binary_dilation as _bd_cc
        zone = _bd_cc(zone, iterations=dil)
        if river_meta is not None:
            zone &= (river_meta <= 0)
        if gap_mask is not None:
            zone &= (gap_mask != 7)
        if _tier_cc is not None:
            zone &= (_tier_cc < 2)
    # S89: 2-3 block fade-OUT on the cap edge (same intent as the rock tiers).
    _cc_fade = int(cc.get("edge_fade_blocks", 0))
    if _cc_fade > 0 and zone.any():
        from scipy.ndimage import distance_transform_edt as _edt_cc
        _cd = _edt_cc(~zone).astype(np.float32)
        _cring = (_cd > 0) & (_cd <= _cc_fade)
        if river_meta is not None:
            _cring &= (river_meta <= 0)
        if gap_mask is not None:
            _cring &= (gap_mask != 7)
        if _tier_cc is not None:
            _cring &= (_tier_cc < 2)
        if _cring.any():
            _ccoin = np.random.default_rng(
                (tile_x * 9176 ^ tile_y * 3251 ^ 0xCCFADE) & 0xFFFFFFFF
            ).random((H, W), dtype=np.float32)
            _cfade = np.clip(1.0 - _cd / float(_cc_fade), 0.0, 1.0)
            zone = zone | (_cring & (_ccoin < _cfade))
    if lithology_tile.shape != (H, W):
        from scipy.ndimage import zoom as _z_cc
        litho = _z_cc(
            lithology_tile,
            (H / lithology_tile.shape[0], W / lithology_tile.shape[1]),
            order=0,
        )
    else:
        litho = lithology_tile
    name_to_id = {
        _gn: int(_gd.get("id", 0))
        for _gn, _gd in litho_cfg.get("groups", {}).items()
    }
    rng = np.random.default_rng(
        (tile_x * 26244 ^ tile_y * 55001 ^ 0xCA9015) & 0xFFFFFFFF
    )
    coin = rng.random((H, W), dtype=np.float32)
    layer_dither = float(cc.get("layer_dither", 0.04))
    for gname, gdata in groups_cfg.items():
        gid = name_to_id.get(gname)
        if gid is None:
            continue
        pal = gdata.get("cap") or []
        if not pal:
            continue
        gm = (litho == gid) & zone
        if not gm.any():
            continue
        _paint_solid_dither(surface_blocks, subsurface_blocks, gm, pal,
                            coin, layer_dither, paint_sub=True)


def _apply_rock_zone_cleanup(
    surface_blocks:    np.ndarray,
    subsurface_blocks: np.ndarray,
    lithology_tile:    np.ndarray | None,
    gap_mask:          np.ndarray | None,
    cfg:               dict,
    tile_x:            int,
    tile_y:            int,
) -> None:
    """S88 walk #9 NEW final pass: on rock_gap (gap_mask==5) pixels,
    overwrite any surviving grass/dirt blocks with the per-litho rock_gap
    palette.  Catches biome-surface/ecotone-dither slip-through that
    survived all prior painting passes on rock pixels.

    Configurable bad-block lists in cfg.lithology.rock_zone_cleanup.
    Subsurface dirt is allowed for granitic (rooted_dirt is in its rock
    palette) — only the literal block names in subsurface_bad_blocks
    get overwritten.  Y-2 through Y-5 column cleanup is chunk_writer's
    responsibility.
    """
    if gap_mask is None or lithology_tile is None:
        return
    cu_cfg = cfg.get("lithology", {}).get("rock_zone_cleanup", {})
    if not cu_cfg.get("enabled", False):
        return
    rock_zone = (gap_mask == 5)
    if not rock_zone.any():
        return

    surface_bad = set(cu_cfg.get("surface_bad_blocks", []))
    sub_bad = set(cu_cfg.get("subsurface_bad_blocks", []))

    surface_bad_mask = rock_zone & np.isin(
        surface_blocks, list(surface_bad)
    )
    sub_bad_mask = rock_zone & np.isin(
        subsurface_blocks, list(sub_bad)
    )
    if not (surface_bad_mask.any() or sub_bad_mask.any()):
        return

    # Resolve lithology to pick the right per-pixel rock_gap palette
    H, W = rock_zone.shape
    if lithology_tile.shape != (H, W):
        from scipy.ndimage import zoom as _z_cu
        litho_at_res = _z_cu(
            lithology_tile,
            (H / lithology_tile.shape[0], W / lithology_tile.shape[1]),
            order=0,
        )
    else:
        litho_at_res = lithology_tile

    groups = cfg.get("lithology", {}).get("groups", {})
    DEFAULT = ["stone", "andesite", "cobblestone"]
    gid_to_pal: dict[int, list] = {}
    for _gname, _gdata in groups.items():
        _gid = int(_gdata.get("id", 0))
        gid_to_pal[_gid] = _gdata.get("palette") or DEFAULT

    rng = np.random.default_rng(
        (tile_x * 0xDEAD ^ tile_y * 0xC1EA1 ^ 0xCC1A) & 0xFFFFFFFF
    )
    for _gid_u in np.unique(litho_at_res[surface_bad_mask | sub_bad_mask]):
        _gid = int(_gid_u)
        _pal = gid_to_pal.get(_gid, DEFAULT)
        _arr = np.asarray(_pal, dtype=object)
        _sm = surface_bad_mask & (litho_at_res == _gid)
        if _sm.any():
            _n = int(_sm.sum())
            surface_blocks[_sm] = _arr[rng.integers(0, len(_pal), size=_n)]
        _sub_m = sub_bad_mask & (litho_at_res == _gid)
        if _sub_m.any():
            _n = int(_sub_m.sum())
            subsurface_blocks[_sub_m] = _arr[rng.integers(0, len(_pal), size=_n)]


def _apply_rock_varnish(
    surface_blocks:    np.ndarray,
    subsurface_blocks: np.ndarray,
    surface_y:         np.ndarray,
    lithology_tile:    np.ndarray | None,
    river_meta:        np.ndarray | None,
    gap_mask:          np.ndarray | None,
    cfg:               dict,
    tile_x:            int,
    tile_y:            int,
    cliff_deg:         np.ndarray | None = None,
    flow_tile:         np.ndarray | None = None,
    varnish_field_tile: np.ndarray | None = None,
) -> None:
    """S88 walk #9.4: PURE FLOW varnish — drip-line streaks on cliff faces.

    Detection:
      1. rock_gap pixel (gap_mask == 5).  Strata-firing area = rock_gap-only
         per walk #6 gating, so rock_gap is the full "rock/strata firing"
         zone.
      2. Drip-flow band: flow_drip_min < flow < flow_drip_max.  Below =
         no water, no stain.  Above = active river/wash channel (varnish
         doesn't fire there; wash painter handles those upstream in the
         pipeline).
      3. Slope fade-in: per-pixel probability ramps from 0 at slope_min_deg
         (= strata floor 32°) to varnish.amp at slope_max_deg (60°).
         "Most-most-most inclined areas" get the densest staining; gentler
         cliffs get progressively less.

    Per-pixel paint of surface_blocks ONLY (no subsurface — varnish is a
    1-block-thick surface stain).  Each group's varnish_palette is 2-3
    shades darker than its rock_gap base in the same color family.
    """
    if lithology_tile is None:
        return
    if _overlay_off(cfg, "varnish"):
        return
    v_cfg = cfg.get("lithology", {}).get("varnish", {})
    if not v_cfg.get("enabled", False):
        return

    # Walk #10.1: VARNISH applies EVERYWHERE (not rock_gap-only).  The
    # varnish_field mask itself encodes the drip-flow + slope gate, so
    # runtime just needs to exclude water/floodplain/snow.
    H, W = surface_y.shape
    _amp = float(v_cfg.get("amp", 0.5))

    excl = np.zeros((H, W), dtype=bool)
    if river_meta is not None:
        excl |= (river_meta > 0)
    if gap_mask is not None:
        excl |= (gap_mask == 4) | (gap_mask == 7)

    # Walk #10/11: prefer PRECOMPUTE MASK varnish_field.tif if available.
    # The mask bakes slope_factor * drip_band per pixel as uint8 0-255.
    # Runtime: convert to float [0,1] and roll a coin per pixel.
    if varnish_field_tile is not None:
        # tile_streamer normalises uint8 -> float32 [0,1]; treat as varnish
        # intensity (= baked slope_factor * drip_band).
        v_intensity = varnish_field_tile.astype(np.float32)
        # Same per-pixel coin as runtime path
        _rng_main = np.random.default_rng(
            (tile_x * 0xDEADBEEF ^ tile_y * 0xCAFE4D ^ 0xBA17) & 0xFFFFFFFF
        )
        _coin = _rng_main.random((H, W), dtype=np.float32)
        candidate = ~excl & (_coin < (_amp * v_intensity))
    else:
        # Fallback: runtime detection (walk #9.4 path)
        if flow_tile is None or cliff_deg is None:
            return  # legacy path requires both
        _slope_min = float(v_cfg.get("slope_min_deg", 32.0))
        _slope_max = float(v_cfg.get("slope_max_deg", 60.0))
        _slope_denom = max(0.1, _slope_max - _slope_min)
        slope_factor = np.clip(
            (cliff_deg - _slope_min) / _slope_denom, 0.0, 1.0
        ).astype(np.float32)
        _flow_min = float(v_cfg.get("flow_drip_min", 0.0001))
        _flow_max = float(v_cfg.get("flow_drip_max", 0.001))
        flow_drip = (flow_tile > _flow_min) & (flow_tile < _flow_max)
        # Walk #10.1: NO rock_gap gate — varnish fires everywhere drip+slope
        candidate = flow_drip & (slope_factor > 0) & ~excl
        if candidate.any():
            _rng_main = np.random.default_rng(
                (tile_x * 0xDEADBEEF ^ tile_y * 0xCAFE4D ^ 0xBA17) & 0xFFFFFFFF
            )
            _slope_coin = _rng_main.random((H, W), dtype=np.float32)
            candidate = candidate & (_slope_coin < (_amp * slope_factor))
    if not candidate.any():
        return

    # ── (4) Optional dilate for visibility ──────────────────────────────
    _dilate = int(v_cfg.get("dilate_blocks", 0))
    if _dilate > 0:
        from scipy.ndimage import binary_dilation as _bd_v
        # Walk #10.1: no rock_zone re-mask (varnish fires everywhere)
        candidate = _bd_v(candidate, iterations=_dilate) & ~excl

    # Lithology resolve
    if lithology_tile.shape != (H, W):
        from scipy.ndimage import zoom as _zv
        litho_at_res = _zv(
            lithology_tile,
            (H / lithology_tile.shape[0], W / lithology_tile.shape[1]),
            order=0,
        )
    else:
        litho_at_res = lithology_tile

    # Per-group varnish_palette LUT
    groups = cfg.get("lithology", {}).get("groups", {})
    gid_to_varnish: dict[int, list] = {}
    for _gname, _gdata in groups.items():
        _gid = int(_gdata.get("id", 0))
        _vp = _gdata.get("varnish_palette")
        if _vp:
            gid_to_varnish[_gid] = _vp

    # Walk #10: candidate already coined upstream (mask path coined via
    # _coin < _amp*v_intensity; runtime path coined via _slope_coin).
    # Apply directly without a second coin.
    apply_zone = candidate
    rng = np.random.default_rng(
        (tile_x * 0xCAFEFADE ^ tile_y * 0xBADDD00D ^ 0xBA17) & 0xFFFFFFFF
    )

    for _gid_u in np.unique(litho_at_res[apply_zone]):
        _gid = int(_gid_u)
        _pal = gid_to_varnish.get(_gid)
        if not _pal:
            continue
        _bm = apply_zone & (litho_at_res == _gid)
        if not _bm.any():
            continue
        _arr = np.asarray(_pal, dtype=object)
        _n = int(_bm.sum())
        surface_blocks[_bm] = _arr[rng.integers(0, len(_pal), size=_n)]
        # subsurface unaffected — varnish is a SURFACE stain (no thicker
        # than 1 block, like real desert varnish coating).


def _apply_cap_edge_stroke(
    surface_blocks:    np.ndarray,
    subsurface_blocks: np.ndarray,
    lithology_tile:    np.ndarray | None,
    river_meta:        np.ndarray | None,
    gap_mask:          np.ndarray | None,
    cfg:               dict,
    tile_x:            int,
    tile_y:            int,
    stroke_width:      int = 4,
) -> None:
    """S88 walk #6 NEW pass: paint cap_palette as a 3-5 block INWARD-only
    stroke at the outer edge of rock_gap regions.  Represents a 'fade out'
    from rocky cliff face to surrounding land — only repaints existing
    rock pixels (does not extend the rocky zone outward).

    Per-pixel lithology -> group cap_palette.
    """
    if gap_mask is None:
        return
    rock_zone = (gap_mask == 5)
    if not rock_zone.any():
        return

    H, W = rock_zone.shape
    from scipy.ndimage import distance_transform_edt as _dt
    # Distance INSIDE rock_zone to the nearest non-rock pixel (the edge).
    dist_to_edge = _dt(rock_zone).astype(np.float32)
    sw = max(1, int(stroke_width))
    stroke_band = rock_zone & (dist_to_edge > 0) & (dist_to_edge <= float(sw))

    excl = np.zeros((H, W), dtype=bool)
    if river_meta is not None:
        excl |= (river_meta > 0)
    excl |= (gap_mask == 4) | (gap_mask == 7)
    stroke_band &= ~excl
    if not stroke_band.any():
        return

    # Resolve lithology
    if lithology_tile is None:
        return
    if lithology_tile.shape != (H, W):
        from scipy.ndimage import zoom as _z
        litho_at_res = _z(
            lithology_tile,
            (H / lithology_tile.shape[0], W / lithology_tile.shape[1]),
            order=0,
        )
    else:
        litho_at_res = lithology_tile

    groups = cfg.get("lithology", {}).get("groups", {})
    gid_to_cap: dict[int, list] = {}
    DEFAULT = ["stone", "cobblestone", "andesite"]
    for _gname, _gdata in groups.items():
        _gid = int(_gdata.get("id", 0))
        gid_to_cap[_gid] = _gdata.get("cap_palette") or DEFAULT

    rng = np.random.default_rng(
        (tile_x * 0xCAFEFADE ^ tile_y * 0xBADDCAFE ^ 0xEDAE) & 0xFFFFFFFF
    )
    for _gid_u in np.unique(litho_at_res[stroke_band]):
        _gid = int(_gid_u)
        _bm = stroke_band & (litho_at_res == _gid)
        if not _bm.any():
            continue
        _pal = gid_to_cap.get(_gid, DEFAULT)
        _arr = np.asarray(_pal, dtype=object)
        _n = int(_bm.sum())
        surface_blocks[_bm] = _arr[rng.integers(0, len(_pal), size=_n)]
        subsurface_blocks[_bm] = _arr[rng.integers(0, len(_pal), size=_n)]


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


def _apply_rock_relief(surface_y, rock_layers_tile, cfg, tile_x, tile_y,
                       cliff_cap_tile=None, biome_grid=None, north_factor=None):
    """S89: un-smooth rocky terrain. The Gaea -> upscale -> MC pipeline leaves
    rock faces too smooth; add low-amplitude, spatially-coherent height noise
    INSIDE rock (tier>=1), with amplitude fading to 0 over the outer
    `edge_fade_blocks` at the rock/land boundary (photoshop inner-stroke) so
    there is NO step-seam against the smooth surrounding land. Mutates
    surface_y IN PLACE at the top of decorate_surface — post Step-9 smoothing,
    pre paint/schematic/column — so blocks, trees and columns all drape over
    the same bumps. World-coord OpenSimplex => seam-free across tiles."""
    rl = cfg.get("lithology", {}).get("rock_layers", {}) if isinstance(cfg, dict) else {}
    if not rl.get("enabled", False) or rock_layers_tile is None:
        return
    rcfg = rl.get("relief", {})
    if not rcfg.get("enabled", False):
        return
    amp = float(rcfg.get("amp_blocks", 2.0))
    if amp <= 0.0:
        return
    scale = max(1.0, float(rcfg.get("scale_blocks", 3.0)))
    H, W = surface_y.shape
    tier = np.round(rock_layers_tile * 255.0).astype(np.int32)
    rock = tier >= 1
    if not rock.any():
        return
    try:
        import opensimplex
        from scipy.ndimage import gaussian_filter as _gf_r
    except Exception:
        return
    sy_f = surface_y.astype(np.float32)
    # ── ADAPTIVE amplitude = base * slope_gain * smooth_gain ──────────────
    # (1) SLOPE gain (per tier): MORE relief on steep rock, LESS on gentle.
    #     tier1 (40-45) low, tier2 (45-50) mid, tier3 (50+) full. Doubles as
    #     the seam guard -- gentle rock stays low-amplitude so there's no step
    #     against flat land (replaces the old distance edge-fade, which was
    #     zeroing thin/steep faces -> "noise not applying in some zones").
    _sg = rcfg.get("slope_gain_by_tier", [0.25, 0.6, 1.0])
    slope_gain = np.zeros((H, W), np.float32)
    slope_gain[tier == 1] = float(_sg[0])
    slope_gain[tier == 2] = float(_sg[1])
    slope_gain[tier >= 3] = float(_sg[2])
    # (2) SMOOTH gain: MORE relief where terrain is ALREADY smooth, LESS where
    #     it is already craggy -- fill in missing roughness, don't pile onto
    #     natural roughness. existing high-freq roughness = |sy - gaussian(sy)|.
    _rsig = float(rcfg.get("roughness_sigma", 4.0))
    _rref = max(0.1, float(rcfg.get("roughness_ref", 2.0)))
    existing_rough = np.abs(sy_f - _gf_r(sy_f, sigma=_rsig))
    smooth_gain = np.clip(1.0 - existing_rough / _rref, 0.0, 1.0).astype(np.float32)
    # (3) optional distance edge-fade (default OFF now; slope_gain handles seams)
    efb = float(rcfg.get("edge_fade_blocks", 0.0))
    if efb > 0.0:
        from scipy.ndimage import distance_transform_edt as _edt_r
        fade = np.clip(_edt_r(rock).astype(np.float32) / efb, 0.0, 1.0)
    else:
        fade = np.float32(1.0)
    # Multi-octave fractal noise (low `scale` + 2 octaves = craggy). Seam-free.
    octaves = max(1, int(rcfg.get("octaves", 1)))
    gain    = float(rcfg.get("octave_gain", 0.5))
    _wx = tile_x * W + np.arange(W, dtype=np.float64)
    _wy = tile_y * H + np.arange(H, dtype=np.float64)
    n = np.zeros((H, W), np.float32)
    _a, _s, _asum = 1.0, scale, 0.0
    for _o in range(octaves):
        _g = opensimplex.OpenSimplex(seed=0x5E11EF + _o * 101)
        n += (_a * _g.noise2array(_wx / _s, _wy / _s)).astype(np.float32)
        _asum += _a; _a *= gain; _s *= 0.5
    n /= max(_asum, 1e-6)                              # back to ~[-1, 1]
    amp_eff = amp * slope_gain * smooth_gain * fade
    relief = amp_eff * n
    delta = np.round(np.where(rock, relief, 0.0)).astype(surface_y.dtype)
    surface_y += delta

    # ── S89: VERY SUBTLE extra relief across the SNOW ZONE, fading out at the
    # snowline. Adds a little terrain variation under the snow so caps aren't
    # smooth domes, but GRADUALLY fades to ZERO at the bottom of the snowline
    # (back to natural-looking terrain). Weight = clip((sy - snow_line) / fade).
    # Snow is placed OVER this later. Separate noise stream. Needs biome_grid.
    _pk = rcfg.get("peak", {})
    if _pk.get("enabled", False) and biome_grid is not None:
        pk_amp = float(_pk.get("amp_blocks", 1.2))
        pk_scale = max(1.0, float(_pk.get("scale_blocks", scale)))
        pk_fade = max(1.0, float(_pk.get("snowline_fade_blocks", 120.0)))
        _snow_line = _build_snow_line(biome_grid, cfg, sy_f, north_factor)
        _alt_w = np.clip((sy_f - _snow_line) / pk_fade, 0.0, 1.0).astype(np.float32)
        if pk_amp > 0.0 and (_alt_w > 0.0).any():
            _g2 = opensimplex.OpenSimplex(seed=0x9EA70 + 7)
            _pn = _g2.noise2array(_wx / pk_scale, _wy / pk_scale).astype(np.float32)
            pk_delta = np.round(pk_amp * _alt_w * _pn).astype(surface_y.dtype)
            surface_y += pk_delta


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
    cliff_cap_tile: np.ndarray | None = None,         # (H, W) float32 [0,1] — cap-rock intensity (S88)
    talus_apron_tile: np.ndarray | None = None,       # (H, W) float32 [0,1] — debris-apron intensity (S88)
    bedrock_drainage_tile: np.ndarray | None = None,  # (H, W) float32 [0,1] — water-cut rock intensity (S88)
    vein_field_tile: np.ndarray | None = None,        # (H, W) float32 [0,1] — vein detection (walk #10/11)
    varnish_field_tile: np.ndarray | None = None,     # (H, W) float32 [0,1] — varnish detection (walk #10/11)
    joint_pattern_tile: np.ndarray | None = None,     # (H, W) float32 [0,1] — basaltic columnar joints (walk #11)
    rock_layers_tile: np.ndarray | None = None,       # (H, W) tiers 0..3 (uint8/255) — S89 rock_layers
    snow_potential_tile: np.ndarray | None = None,    # (H, W) float32 [0,1] — S89 continuous snow potential (depth-snow)
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

    # S89: ROCK RELIEF — un-smooth rocky terrain. MUST run AFTER the dune-flatten
    # + biome-boundary Y-smoothers above: those run a sigma-8 x3 gaussian on
    # surface_y across a 24-block buffer at EVERY biome boundary, and mountain
    # cliffs routinely sit on biome boundaries -> they were wiping the relief on
    # exactly the rock near boundaries ("zero noise" on cliffs). Applied here it
    # is the LAST surface_y change in decorate, so paint/schematic/column all
    # drape over the bumps and nothing smooths them back out. Mutates in place;
    # no-op unless rock_layers + relief enabled.
    _apply_rock_relief(surface_y, rock_layers_tile, cfg, tile_x, tile_y,
                       cliff_cap_tile=cliff_cap_tile, biome_grid=biome_grid,
                       north_factor=(eco_grads.north_factor if (eco_grads is not None
                                     and hasattr(eco_grads, "north_factor")) else None))

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
            # S88 walk #4d: PAINTED `lithology.tif` is the SOLE source of truth.
            # No biome fallback via zone_to_group.  Unpainted pixels (gid=0)
            # and tiles without a lithology mask both use _DEFAULT_PAL.
            # Previously the biome fallback hid lithology painting bugs by
            # silently falling back to biome-derived defaults — that masking
            # is now removed so palette mistakes are visible immediately.
            _litho_cfg = cfg.get("lithology", {}) if isinstance(cfg, dict) else {}
            _groups = _litho_cfg.get("groups", {})
            _litho_at_res = None
            if lithology_tile is not None:
                if lithology_tile.shape != (H, W):
                    from scipy.ndimage import zoom as _sd_zoom_outer
                    _zh_outer = H / lithology_tile.shape[0]
                    _zw_outer = W / lithology_tile.shape[1]
                    _litho_at_res = _sd_zoom_outer(
                        lithology_tile, (_zh_outer, _zw_outer), order=0)
                else:
                    _litho_at_res = lithology_tile

            # S89: when rock_layers is enabled its tier mask is the SOLE rock
            # presence — swap gap==5 to the rock_layers extent so rock_px / wash
            # / sand / snow / cleanup below all key off the new rock, and the
            # legacy rock_gap extent is dropped.
            _rl_enabled = bool(
                cfg.get("lithology", {}).get("rock_layers", {}).get("enabled", False)
            )
            if _rl_enabled and rock_layers_tile is not None:
                _rl_tier = np.round(rock_layers_tile * 255.0).astype(np.int32)
                _gap[_gap == 5] = 0                       # drop old rock_gap extent
                _gap[(_rl_tier >= 1) & (_gap == 0)] = 5   # claim new rock from 'none'
                del _rl_tier

            rock_px = _gap == 5
            if rock_px.any() and not _rl_enabled:
                _rng = np.random.default_rng(tile_x * 48271 ^ tile_y * 31337 ^ 0xB10C)
                _scatter = _rng.random((H, W)).astype(np.float32)
                _DEFAULT_PAL = ["stone", "andesite", "granite", "diorite"]

                # Build group-id → palette LUT from config.
                _gid_to_pal: dict[int, list] = {}
                for _gname, _gdata in _groups.items():
                    _gid = int(_gdata.get("id", 0))
                    _pal_g = _gdata.get("palette") or _DEFAULT_PAL
                    _gid_to_pal[_gid] = _pal_g

                # PAINTED-ONLY path.  No biome fallback.
                # gid==0 (unpainted) and missing lithology_tile both use _DEFAULT_PAL.
                if _litho_at_res is not None:
                    _unique_gids = np.unique(_litho_at_res[rock_px])
                    for _gid in _unique_gids:
                        _gid = int(_gid)
                        _bm_lith = rock_px & (_litho_at_res == _gid)
                        if not _bm_lith.any():
                            continue
                        _pal = _gid_to_pal.get(_gid, _DEFAULT_PAL)
                        _n = len(_pal)
                        for _i, _blk in enumerate(_pal):
                            _lo = _i / _n; _hi = (_i + 1) / _n
                            _band = _bm_lith & (_scatter >= _lo) & (_scatter < _hi)
                            if _band.any():
                                surface_blocks[_band] = _blk
                        subsurface_blocks[_bm_lith] = _pal[0]
                else:
                    # No lithology mask available — default palette globally
                    _pal = _DEFAULT_PAL
                    _n = len(_pal)
                    for _i, _blk in enumerate(_pal):
                        _lo = _i / _n; _hi = (_i + 1) / _n
                        _band = rock_px & (_scatter >= _lo) & (_scatter < _hi)
                        if _band.any():
                            surface_blocks[_band] = _blk
                    subsurface_blocks[rock_px] = _pal[0]

                # S88 walk #4b: wash painter MOVED to after strata-surface
                # (see below this rock_px block).  Strata used to overwrite
                # washes; now wash fires AFTER strata so wash overrides strata
                # on flow-channel cells.  User-spec order:
                #   rock_gap -> strata -> wash -> bedrock -> talus -> veins -> cap
                del _rng, _scatter

            # ── S89: ROCK_LAYERS tier paint (base rock) ────────────────────
            # When enabled, this REPLACES the legacy rock_px palette block
            # above + _apply_strata_surface + _apply_basaltic_joints below
            # (all three early-return / are skipped under the flag). Veins /
            # wash / talus / varnish / cliff_cap still overlay it downstream.
            _apply_rock_layers(
                surface_blocks    = surface_blocks,
                subsurface_blocks = subsurface_blocks,
                lithology_tile    = lithology_tile,
                rock_layers_tile  = rock_layers_tile,
                cfg               = cfg,
                tile_x            = tile_x,
                tile_y            = tile_y,
                talus_apron_tile  = talus_apron_tile,
            )

            # ── S88 walk #2: STRATA SURFACE PAINT ──────────────────────────
            # Strata bands extend up through surface_blk + sub_blk on slopes
            # >= lithology.strata.surface_min_deg (default 32°).  Fires
            # REGARDLESS of gap_mask -- on a steep cliff face that misses
            # rock_gap detection, strata still wins.  Y-band schedule matches
            # chunk_writer's basement fill exactly so bands continue smoothly
            # from cliff TOP through the FACE into the basement -- no seam at
            # the dirt layer.
            _apply_strata_surface(
                surface_blocks    = surface_blocks,
                subsurface_blocks = subsurface_blocks,
                surface_y         = surface_y,
                biome_grid        = biome_grid,
                lithology_tile    = lithology_tile,
                cliff_deg         = cliff_deg,
                river_meta        = river_meta,
                gap_mask          = _gap,
                cfg               = cfg,
                tile_x            = tile_x,
                tile_y            = tile_y,
                aspect            = eco_grads.aspect if eco_grads is not None else None,
            )

            # ── Walk #11: BASALTIC COLUMNAR JOINTS ──────────────────────────
            # Paint vertical joint planes (darker basalt block) on basaltic
            # rock_gap pixels using joint_pattern.tif.  Produces visible
            # vertical fractures every ~3 blocks on basalt cliff faces.
            _apply_basaltic_joints(
                surface_blocks    = surface_blocks,
                subsurface_blocks = subsurface_blocks,
                lithology_tile    = lithology_tile,
                gap_mask          = _gap,
                joint_pattern_tile= joint_pattern_tile,
                cfg               = cfg,
                tile_x            = tile_x,
                tile_y            = tile_y,
            )

            # ── Walk #12: CONCAVITY moved LATER (after talus, before varnish)
            # per realistic-geology pipeline order: concavity captures sediment
            # accumulated IN local depressions by water/gravity — should
            # OVERLAY erosional features (wash/talus) not be hidden under them.
            # Old call relocated to after talus_apron painter below.

            # ── Walk #10.1: VEINS moved BEFORE wash ────────────────────────
            # User layer order: surface -> rock_gap -> strata -> concavity ->
            # VEINS -> wash -> bedrock -> talus -> varnish -> cap -> cleanup.
            # Veins now paint first so wash/bedrock/talus can paint over
            # them on overlapping pixels (rivers + drainage win over veins).
            _apply_strata_veins_surface(
                surface_blocks    = surface_blocks,
                subsurface_blocks = subsurface_blocks,
                surface_y         = surface_y,
                biome_grid        = biome_grid,
                lithology_tile    = lithology_tile,
                cliff_deg         = cliff_deg,
                river_meta        = river_meta,
                gap_mask          = _gap,
                cfg               = cfg,
                tile_x            = tile_x,
                tile_y            = tile_y,
                vein_field_tile   = vein_field_tile,
            )

            # ── S89: FOLIATION RIBS (real cross-cutting flow lines) ────────
            # Ribs follow ACTUAL drainage from the flow mask -- NOT procedural --
            # but only the FINE tributaries that TRAVERSE the slope (run across it),
            # never the trunk washes that plunge straight down. Discriminator:
            #   a channel runs along accumulation ridges, perp to grad(flow);
            #   a straight-DOWN channel keeps grad(flow) PERP to the fall line
            #     grad(height) -> |cos(grad flow, grad height)| LOW;
            #   a TRAVERSING channel has grad(flow) PARALLEL to grad(height)
            #     -> |cos| HIGH.  Keep high-cos channel pixels.
            # Band-limited to flow in [min_flow, wash_min_flow) so ribs live
            # BETWEEN the washes. Painted thin with each group's MID tier.
            if (flow_tile is not None and rock_px.any() and not _overlay_off(cfg, "wash")
                    and cfg.get("washes", {}).get("foliated", {}).get("enabled", True)):
                _fcfg = cfg.get("washes", {}).get("foliated", {})
                _fol_lo = float(_fcfg.get("min_flow", 0.0008))
                _fol_hi = float(cfg.get("washes", {}).get("min_flow", 0.003))
                _cos_thr = float(_fcfg.get("cross_cos", 0.5))
                _fsig = float(_fcfg.get("smooth_sigma", 1.0))
                from scipy.ndimage import gaussian_filter as _gf_fol
                _ff = flow_tile.astype(np.float32)
                _chan = rock_px & (_ff > _fol_lo) & (_ff < _fol_hi)
                # gradient alignment: |cos(grad(flow), grad(height))|
                _gfy, _gfx = np.gradient(_gf_fol(_ff, _fsig))
                _ghy, _ghx = np.gradient(surface_y.astype(np.float32))
                _dot = _gfy * _ghy + _gfx * _ghx
                _mag = np.sqrt((_gfy * _gfy + _gfx * _gfx)
                               * (_ghy * _ghy + _ghx * _ghx)) + 1e-6
                _cos = np.abs(_dot / _mag)
                _fol_zone = _chan & (_cos > _cos_thr)
                _fol_min_deg = float(_fcfg.get("min_slope_deg", 0.0))
                if _fol_min_deg > 0.0 and cliff_deg is not None:
                    _fol_zone &= (cliff_deg >= _fol_min_deg)
                if _fol_zone.any():
                    _fol_coin = np.random.default_rng(
                        (tile_x * 99991 ^ tile_y * 49993 ^ 0xF0117ED) & 0xFFFFFFFF
                    ).random((H, W)).astype(np.float32)
                    _fol_dither = float(_fcfg.get("layer_dither", 0.5))
                    # CONTRAST SWAP: a rib defaults to its group's MID tier, but
                    # where it crosses a pixel whose NATIVE rock_layers tier is
                    # already MID (mid-on-mid = invisible), swap to DARK so the
                    # rib always contrasts the rock it cuts across. tier: 1=dark,
                    # 2=mid, 3=light.
                    _fol_tier = (np.round(rock_layers_tile.astype(np.float32) * 255.0
                                          ).astype(np.int32)
                                 if rock_layers_tile is not None else None)
                    _fol_groups = cfg.get("lithology", {}).get("rock_layers", {}).get("groups", {})
                    _fol_n2id = {gn: int(gd.get("id", 0))
                                 for gn, gd in cfg.get("lithology", {}).get("groups", {}).items()}
                    for _fgn, _fgd in _fol_groups.items():
                        _fmid = _fgd.get("mid")
                        _fdark = _fgd.get("dark")
                        _flight = _fgd.get("light")
                        _fgid = _fol_n2id.get(_fgn)
                        if not _fmid or _fgid is None:
                            continue
                        _fbm = (_fol_zone & (_litho_at_res == _fgid)
                                if _litho_at_res is not None else _fol_zone)
                        if not _fbm.any():
                            continue
                        if _fol_tier is not None:
                            # S89 walk: TIER-SPECIFIC rib so it always reads against
                            # the rock it crosses. DARK zone -> LIGHT rib; LIGHT zone
                            # -> DARK rib; MID zone -> the most-contrasting tier for
                            # that palette (per-group `rib_mid_contrast` = dark|light).
                            _mid_rib = (_flight
                                        if _fgd.get("rib_mid_contrast", "dark") == "light"
                                        else _fdark)
                            for _msk, _pal in (
                                (_fbm & (_fol_tier == 1), _flight),   # dark zone
                                (_fbm & (_fol_tier == 2), _mid_rib),  # mid zone
                                (_fbm & (_fol_tier >= 3), _fdark),    # light zone
                            ):
                                if _pal and _msk.any():
                                    _paint_solid_dither(surface_blocks, subsurface_blocks,
                                                        _msk, _pal, _fol_coin,
                                                        _fol_dither, paint_sub=True)
                        else:
                            _paint_solid_dither(surface_blocks, subsurface_blocks,
                                                _fbm, _fmid, _fol_coin, _fol_dither,
                                                paint_sub=True)

            # ── S88 walk #4b: WASH PAINTER (moved from inside rock_px) ─────
            # Per user direction: rock_gap -> strata -> WASH -> bedrock -> talus
            #                     -> veins -> cap.
            # Strata-surface paints first on slope >= 28°.  Wash now fires
            # AFTER and overrides strata on rock cells with high flow.
            if flow_tile is not None and rock_px.any() and not _overlay_off(cfg, "wash"):
                _wcfg = cfg.get("washes", {}) if isinstance(cfg, dict) else {}
                _wash_min_flow = float(_wcfg.get("min_flow", 0.002))
                _wash_zone_core = rock_px & (flow_tile > _wash_min_flow)
                if not _wash_zone_core.any():
                    _wash_zone = _wash_zone_core
                else:
                    # S89: FLOW-PROPORTIONAL WIDTH. Each wash pixel's half-width is
                    # set by the FLOW at its NEAREST channel cell, so the wash is
                    # ~2-3 wide at the summit (low flow) and widens NATURALLY toward
                    # the base (high flow) like a real drainage -- replacing the old
                    # fixed dilation + stepped fan. width_min/max are radii (blocks);
                    # diameter ~= 2*radius+1.
                    from scipy.ndimage import distance_transform_edt as _dt
                    _w_min = float(_wcfg.get("width_min", 1.0))      # ~3 wide at summit
                    _w_max = float(_wcfg.get("width_max", 5.0))      # ~11 wide at base
                    # flow_tile is RAW uint16 accumulation (0..65535), heavily
                    # log-skewed -> ramp width on LOG(flow) between flow_lo (summit,
                    # low accumulation -> width_min) and flow_hi (base, high
                    # accumulation -> width_max). Linear-on-raw saturated instantly.
                    _f_lo = max(1.0, float(_wcfg.get("width_flow_lo", 30.0)))
                    _f_hi = max(_f_lo + 1.0, float(_wcfg.get("width_flow_hi", 5000.0)))
                    _dist, _idx = _dt(~_wash_zone_core, return_indices=True)
                    _dist = _dist.astype(np.float32)
                    _near_flow = flow_tile[_idx[0], _idx[1]].astype(np.float32)
                    _lf = np.log(np.maximum(_near_flow, 1.0))
                    _wt = np.clip((_lf - np.log(_f_lo)) / (np.log(_f_hi) - np.log(_f_lo)), 0.0, 1.0)
                    _w_rad = (_w_min + (_w_max - _w_min) * _wt).astype(np.float32)
                    _wash_zone = (_dist <= _w_rad) & rock_px
                    # S89: slight HORIZONTAL wander (~3-4 blocks) so the channel
                    # meanders instead of running dead-straight. Smooth world-coord
                    # displacement field warps the zone left/right via map_coordinates.
                    _wob = float(_wcfg.get("dither_blocks", 0.0))
                    if _wob > 0.0 and _wash_zone.any():
                        from scipy.ndimage import map_coordinates as _mc_wash
                        _wdx = (_noise_tile(_ecotone_meander_gen(cfg), H, W,
                                            px_off, py_off, scale=40.0, octaves=2)
                                - 0.5) * 2.0 * _wob
                        _yy, _xx = np.mgrid[0:H, 0:W]
                        _sx = np.clip(_xx.astype(np.float32) - _wdx, 0, W - 1)
                        _wash_zone = _mc_wash(_wash_zone.astype(np.float32),
                                              [_yy.astype(np.float32), _sx],
                                              order=1, mode="nearest") > 0.4
                        _wash_zone &= rock_px
                    # S89 EDGE salt & pepper: speckle out ONLY the outer
                    # `edge_fade_blocks` of the wash so the rim dissolves into
                    # little specks; the core stays solid. keep-prob ramps 0 at the
                    # rim -> 1 at the band's inner edge.
                    _efade = float(_wcfg.get("edge_fade_blocks", 3.0))
                    if _efade > 0.0 and _wash_zone.any():
                        _ef_rng = np.random.default_rng(tile_x * 13 ^ tile_y * 7 ^ 0xD1AF)
                        _ef_coin = _ef_rng.random((H, W)).astype(np.float32)
                        _from_edge = (_w_rad - _dist)          # blocks in from the rim
                        # only speckle the outer band AND never the centerline
                        # (_dist < 1), so thin washes (radius ~2) keep a solid core.
                        _band = _wash_zone & (_from_edge < _efade) & (_dist >= 1.0)
                        _keep_p = np.clip(_from_edge / np.maximum(_efade, 0.5), 0.0, 1.0)
                        _wash_zone &= ~(_band & (_ef_coin > _keep_p))
                if _wash_zone.any():
                    _DEFAULT_WASH_PAL = ["gravel", "coarse_dirt", "sand"]
                    _wash_rng = np.random.default_rng(
                        tile_x * 48271 ^ tile_y * 31337 ^ 0x4A5E)
                    _gid_to_wash: dict[int, list] = {}
                    for _gname, _gdata in _groups.items():
                        _gid = int(_gdata.get("id", 0))
                        _wp = _gdata.get("wash_palette") or _DEFAULT_WASH_PAL
                        _gid_to_wash[_gid] = _wp

                    # PAINTED-ONLY path.  gid==0 + no lithology = _DEFAULT_WASH_PAL.
                    if _litho_at_res is not None:
                        for _gid in np.unique(_litho_at_res[_wash_zone]):
                            _gid = int(_gid)
                            _bm_w = _wash_zone & (_litho_at_res == _gid)
                            if not _bm_w.any():
                                continue
                            _wp = _gid_to_wash.get(_gid, _DEFAULT_WASH_PAL)
                            _n_pix = int(_bm_w.sum())
                            _wp_arr = np.asarray(_wp, dtype=object)
                            surface_blocks[_bm_w] = _wp_arr[
                                _wash_rng.integers(0, len(_wp), size=_n_pix)]
                            subsurface_blocks[_bm_w] = _wp_arr[
                                _wash_rng.integers(0, len(_wp), size=_n_pix)]
                    else:
                        _wp = _DEFAULT_WASH_PAL
                        _n_pix = int(_wash_zone.sum())
                        _wp_arr = np.asarray(_wp, dtype=object)
                        surface_blocks[_wash_zone] = _wp_arr[
                            _wash_rng.integers(0, len(_wp), size=_n_pix)]
                        subsurface_blocks[_wash_zone] = _wp_arr[
                            _wash_rng.integers(0, len(_wp), size=_n_pix)]
                    del _wash_rng
                del _wash_zone

            # ── S88 terrain-derived painters ───────────────────────────────
            # bedrock_drainage / talus_apron / cliff_cap, in that order.
            # Each uses its own intensity_threshold from config; each shares
            # the same lithology-group→palette pattern as the wash painter
            # above.  Snake-cased palette keys: cap_palette, talus_palette,
            # bedrock_drainage_palette.
            #
            # Exclusions per painter (so we don't paint over water, snow, etc):
            #   bedrock_drainage  → skip river_meta>0, gap==4 (floodplain)
            #   talus_apron       → skip river_meta>0, gap==4 (floodplain)
            #   cliff_cap         → skip river_meta>0, gap==7 (snow wins)
            #
            # All three honour the per-pixel lithology group via
            # _litho_at_res (set above) with biome fallback for unpainted
            # pixels (lithology id 0).
            _s88_lcfg = cfg.get("lithology", {}) if isinstance(cfg, dict) else {}
            _s88_groups = _s88_lcfg.get("groups", {})

            def _s88_apply_painter(
                mask_tile,
                cfg_section: dict,
                palette_key: str,
                paint_subsurface: bool,
                exclusion_mask: np.ndarray,
                rng_salt: int,
                default_palette: list[str],
            ) -> None:
                """Paint `palette_key` per-pixel where mask_tile >= threshold.
                exclusion_mask: pixels where painting is forbidden.
                Walk #4d: lithology.tif is the SOLE source of truth.  No
                biome fallback.  Unpainted pixels (gid=0) or missing mask =
                default_palette globally."""
                if mask_tile is None:
                    return
                intensity_threshold = int(cfg_section.get("intensity_threshold", 64))
                intensity_byte = (mask_tile * 255.0).astype(np.int32)
                paint_zone = (intensity_byte >= intensity_threshold) & ~exclusion_mask
                # Walk #6: optional runtime dilation (used by cliff_cap to
                # widen rock-cap coverage without rebuilding the mask).
                _dilate_blocks = int(cfg_section.get("dilate_blocks", 0))
                if _dilate_blocks > 0 and paint_zone.any():
                    from scipy.ndimage import binary_dilation as _bd_painter
                    paint_zone = (
                        _bd_painter(paint_zone, iterations=_dilate_blocks)
                        & ~exclusion_mask
                    )
                if not paint_zone.any():
                    return
                gid_to_pal_local: dict[int, list] = {}
                for _gn, _gd in _s88_groups.items():
                    _gid = int(_gd.get("id", 0))
                    gid_to_pal_local[_gid] = (
                        _gd.get(palette_key) or default_palette
                    )

                _s88_rng = np.random.default_rng(
                    tile_x * 48271 ^ tile_y * 31337 ^ rng_salt)

                def _paint_block(_bm, _pal):
                    _arr = np.asarray(_pal, dtype=object)
                    _n_px = int(_bm.sum())
                    surface_blocks[_bm] = _arr[
                        _s88_rng.integers(0, len(_pal), size=_n_px)
                    ]
                    if paint_subsurface:
                        subsurface_blocks[_bm] = _arr[
                            _s88_rng.integers(0, len(_pal), size=_n_px)
                        ]

                if _litho_at_res is not None:
                    for _gid_u in np.unique(_litho_at_res[paint_zone]):
                        _gid = int(_gid_u)
                        _bm = paint_zone & (_litho_at_res == _gid)
                        if not _bm.any():
                            continue
                        _pal = gid_to_pal_local.get(_gid, default_palette)
                        _paint_block(_bm, _pal)
                else:
                    _paint_block(paint_zone, default_palette)

                # Walk #10: optional 3x3 median/mode filter post-paint to
                # smooth salt-and-pepper per-pixel palette pick into patches
                # (bedrock_drainage was 'spotty' per user — this consolidates
                # adjacent same-block clusters).
                if cfg_section.get("median_filter_post", False) and paint_zone.any():
                    # Map block names to int indices per palette, mode-filter,
                    # map back.  Operates on the paint_zone only.
                    _pal_set = set()
                    for _pal_v in gid_to_pal_local.values():
                        _pal_set.update(_pal_v)
                    _pal_set.update(default_palette)
                    _pal_list = sorted(_pal_set)
                    _name_to_idx = {n: i for i, n in enumerate(_pal_list)}
                    _N = len(_pal_list)
                    # Build int index array for surface_blocks at paint_zone
                    _idx_arr = np.full((H, W), -1, dtype=np.int16)
                    for _i, _n in enumerate(_pal_list):
                        _idx_arr[paint_zone & (surface_blocks == _n)] = _i
                    # 3x3 mode filter via scipy.ndimage.generic_filter is slow;
                    # use a per-block dilation-comparison instead: count each
                    # palette index in 3x3 windows, pick majority.
                    from scipy.ndimage import uniform_filter
                    _winner_idx = _idx_arr.copy()
                    _winner_cnt = np.zeros((H, W), dtype=np.float32)
                    for _i in range(_N):
                        _occ = (_idx_arr == _i).astype(np.float32)
                        _cnt3 = uniform_filter(_occ, size=3, mode="nearest") * 9.0
                        _better = _cnt3 > _winner_cnt
                        _winner_idx[_better] = _i
                        _winner_cnt[_better] = _cnt3[_better]
                    # Apply winner where paint_zone
                    _pal_arr_obj = np.asarray(_pal_list, dtype=object)
                    _valid = paint_zone & (_winner_idx >= 0)
                    surface_blocks[_valid] = _pal_arr_obj[_winner_idx[_valid]]
                    if paint_subsurface:
                        subsurface_blocks[_valid] = _pal_arr_obj[_winner_idx[_valid]]

            # Build per-painter exclusion masks
            _river_excl = (river_meta > 0) if river_meta is not None else np.zeros((H, W), dtype=bool)
            _flood_excl = (_gap == 4)
            _snow_excl = (_gap == 7)

            # 1. bedrock_drainage — paints surface + sub (uniform rock column)
            _s88_apply_painter(
                mask_tile=(None if _overlay_off(cfg, "bedrock_drainage") else bedrock_drainage_tile),
                cfg_section=_s88_lcfg.get("bedrock_drainage", {}),
                palette_key="bedrock_drainage_palette",
                paint_subsurface=True,
                exclusion_mask=_river_excl | _flood_excl,
                rng_salt=0xB3D4,
                default_palette=["andesite", "stone", "gravel"],
            )

            # 2. talus_apron — surface only (debris layer over dirt).
            # S89: when rock_layers is enabled the legacy generic painter is
            # disabled (mask_tile=None) and _apply_talus (grain-sort + depth)
            # runs instead.
            _s88_apply_painter(
                mask_tile=(None if _rl_enabled else talus_apron_tile),
                cfg_section=_s88_lcfg.get("talus", {}),
                palette_key="talus_palette",
                paint_subsurface=False,
                exclusion_mask=_river_excl | _flood_excl,
                rng_salt=0x7A1F,
                default_palette=["cobblestone", "gravel", "coarse_dirt"],
            )
            _apply_talus(
                surface_blocks    = surface_blocks,
                subsurface_blocks = subsurface_blocks,
                lithology_tile    = lithology_tile,
                talus_apron_tile  = talus_apron_tile,
                cfg               = cfg,
                tile_x            = tile_x,
                tile_y            = tile_y,
                river_meta        = river_meta,
                gap_mask          = _gap,
            )

            # 2b. Walk #10.1: VEINS moved to BEFORE wash (between strata and
            # wash, with concavity also early).  Old call site here removed.

            # 2c. Walk #12: CONCAVITY moved here (after talus, before varnish).
            # Real-geology order: concavity captures sediment ACCUMULATED in
            # local depressions by water/gravity — must overlay wash/bedrock/
            # talus, not be hidden under them.
            _apply_concavity_drainage(
                surface_blocks    = surface_blocks,
                subsurface_blocks = subsurface_blocks,
                surface_y         = surface_y,
                lithology_tile    = lithology_tile,
                river_meta        = river_meta,
                gap_mask          = _gap,
                cfg               = cfg,
                tile_x            = tile_x,
                tile_y            = tile_y,
                cliff_deg         = cliff_deg,
            )

            # 2d. Walk #8 NEW: rock varnish — per-litho stain in crevices/
            #     corners.  Each group's varnish_palette is relatively
            #     darker than its rock_gap base, reads as natural mineral
            #     staining (subtle on dark basaltics, dramatic on white
            #     limestone — iconic Mojave karst varnish).
            #
            # WALK #8 REMOVED: _apply_cap_edge_stroke (the 4-block cap-
            # palette ring at rock_gap edges was the real cause of "cap
            # covering everything" in walk #6/#7, not the cap painter
            # itself).  Cap painter restored to walk #6's larger
            # proportions (threshold=8, dilate=12) via config.
            _apply_rock_varnish(
                surface_blocks    = surface_blocks,
                subsurface_blocks = subsurface_blocks,
                surface_y         = surface_y,
                lithology_tile    = lithology_tile,
                river_meta        = river_meta,
                gap_mask          = _gap,
                cfg               = cfg,
                tile_x            = tile_x,
                tile_y            = tile_y,
                cliff_deg         = cliff_deg,
                flow_tile         = flow_tile,
                varnish_field_tile= varnish_field_tile,
            )

            # 3. cliff_cap — surface + sub (cap rock all the way down 1 layer).
            # S89: when rock_layers enabled the legacy generic cap painter is
            # disabled (mask_tile=None) and _apply_cliff_cap (convexity-exposure
            # scoured palette) runs instead. The GC/tree kill below is unchanged.
            _s88_apply_painter(
                mask_tile=(None if _rl_enabled else cliff_cap_tile),
                cfg_section=_s88_lcfg.get("cliff_cap", {}),
                palette_key="cap_palette",
                paint_subsurface=True,
                exclusion_mask=_river_excl | _snow_excl,
                rng_salt=0xCAFE,
                default_palette=["tuff", "andesite", "cobblestone"],
            )
            _apply_cliff_cap(
                surface_blocks    = surface_blocks,
                subsurface_blocks = subsurface_blocks,
                lithology_tile    = lithology_tile,
                cliff_cap_tile    = cliff_cap_tile,
                cfg               = cfg,
                tile_x            = tile_x,
                tile_y            = tile_y,
                river_meta        = river_meta,
                gap_mask          = _gap,
                rock_layers_tile  = rock_layers_tile,
            )

            # Walk #12: KILL GROUND_COVER on cap pixels (cap is bare rock,
            # no grass/flowers/bushes should grow on a peak top).  Computes
            # the same paint_zone as the cap painter just did and zeros
            # ground_cover at those pixels.  Tree suppression handled
            # downstream in schematic_placement via gap_mask + cliff_cap_tile.
            if (
                cliff_cap_tile is not None
                and _s88_lcfg.get("cliff_cap", {}).get("kill_ground_cover", False)
                and ground_cover is not None
            ):
                _cc_thr = int(_s88_lcfg.get("cliff_cap", {}).get("intensity_threshold", 8))
                _cc_dil = int(_s88_lcfg.get("cliff_cap", {}).get("dilate_blocks", 0))
                _cc_intensity = (cliff_cap_tile * 255.0).astype(np.int32)
                _cc_zone = (_cc_intensity >= _cc_thr) & ~(_river_excl | _snow_excl)
                if _cc_dil > 0 and _cc_zone.any():
                    from scipy.ndimage import binary_dilation as _bd_cck
                    _cc_zone = _bd_cck(_cc_zone, iterations=_cc_dil)
                if _cc_zone.any():
                    ground_cover[_cc_zone] = ""

            # ── Walk #9 NEW: rock_zone_cleanup ───────────────────────────
            # Final pass: on rock_gap (gap_mask==5) pixels, overwrite any
            # surviving GRASS_FAMILY surface blocks or DIRT_FAMILY subsurface
            # blocks with the per-litho rock_gap palette.  Catches grass/dirt
            # slip-through from ecotone dither, biome surface paint, etc.
            # Y-2..Y-5 cleanup handled separately by chunk_writer.
            _apply_rock_zone_cleanup(
                surface_blocks    = surface_blocks,
                subsurface_blocks = subsurface_blocks,
                lithology_tile    = lithology_tile,
                gap_mask          = _gap,
                cfg               = cfg,
                tile_x            = tile_x,
                tile_y            = tile_y,
            )

            # ── S89: GRASS TERRACES — soil/grass on flat ledges of rocky
            # slopes (after the rock painters, before snow). Greens up the
            # benches where soil realistically lands; steep faces stay bare.
            _apply_grass_terraces(
                surface_blocks    = surface_blocks,
                subsurface_blocks = subsurface_blocks,
                ground_cover      = ground_cover,
                surface_y         = surface_y,
                cliff_deg         = cliff_deg,
                rock_px           = rock_px,
                biome_grid        = biome_grid,
                flow_tile         = flow_tile,
                river_meta        = river_meta,
                snow_potential    = snow_potential_tile,
                cfg               = cfg,
                tile_x            = tile_x,
                tile_y            = tile_y,
            )

            del _river_excl, _flood_excl, _snow_excl

            # ── Snow caps (gap==7): snow_block replacement (S56 simplified) ──
            # Gaea dusting mask drives gap==7. All snow pixels get snow_block.
            # Dither edges are baked into the mask at 50k by rebuild_gaea_gaps.py
            # (blue-noise dither), so no per-pixel probability ramp needed here.
            # S85: SNOWY_BOREAL_TAIGA + FROZEN_FLATS exempted — SBT's native podzol
            # surface supports foliage and snow_carpet (vanilla snow[layers=1])
            # provides the snowy visual on top.  FF is the Tundra Valley design
            # (grass_block + scattered snow_carpet); forcing snow_block here would
            # break the Tundra Valley palette + nuke its rich GC palette.
            # S89 depth-snow: when enabled, the potential-driven _apply_depth_snow
            # pass (later) owns ALL snow placement (caps + drifts + carpet) so the
            # flat snow_block here would wrongly bury drift pixels under solid snow.
            # Skip the legacy flat fill; fall through only when depth is OFF.
            _dcfg_gap = cfg.get("snow_physics", {}).get("depth", {})
            _depth_on = bool(_dcfg_gap.get("enabled", False)) \
                        and snow_potential_tile is not None \
                        and float(np.asarray(snow_potential_tile).max()) > 0.0
            # In gully_only mode the Gaea gap==7 is the BASE snow, so this
            # consumer must run (depth-snow only adds high-gully fingers).
            _gully_only_gap = bool(_dcfg_gap.get("gully_only", False))
            _run_gaea_gap = (not _depth_on) or _gully_only_gap
            snow_px = (_gap == 7) & _run_gaea_gap
            if snow_px.any():
                _snow_exempt = (
                    (biome_grid == "SNOWY_BOREAL_TAIGA") |
                    (biome_grid == "FROZEN_FLATS")
                )
                snow_px = snow_px & ~_snow_exempt
            # S89 PER-BIOME snow line + PATCHY TRANSITION BAND. Instead of a hard
            # altitude cutoff, snow survival across a +/-transition_blocks band is
            # decided by MICRO-TERRAIN, not a coin: lingers in fine hollows (curv),
            # shade (north aspect) and physics drifts/couloirs (snow_potential =
            # wind-shelter Sx + curvature); melts on bumps, sun, scoured ground.
            # The biome base line stays dominant -> climate ordering preserved.
            if snow_px.any():
                _snow_cand = snow_px.copy()   # gap==7 candidate pool, pre-band
                _nf_sl = (eco_grads.north_factor if (eco_grads is not None
                          and hasattr(eco_grads, "north_factor")) else None)
                _eff_line = _build_snow_line(biome_grid, cfg, surface_y, _nf_sl)
                _slc = cfg.get("snow_lines", {})
                _band = max(1.0, float(_slc.get("transition_blocks", 25.0)))
                _syf = surface_y.astype(np.float32)
                _t = np.clip((_syf - _eff_line + _band) / (2.0 * _band), 0.0, 1.0)
                from scipy.ndimage import gaussian_filter as _gf_band
                _fine = _gf_band(_syf, 1.5) - _syf            # +concave / -convex
                _micro = (float(_slc.get("micro_curv_coeff", 0.40))
                          * np.clip(_fine / float(_slc.get("micro_curv_ref", 2.0)),
                                    -1.0, 1.0)).astype(np.float32)
                if _nf_sl is not None:
                    _micro = _micro + float(_slc.get("micro_aspect_coeff", 0.25)) \
                             * (_nf_sl.astype(np.float32) - 0.5) * 2.0
                if (snow_potential_tile is not None
                        and float(np.asarray(snow_potential_tile).max()) > 0.0):
                    _micro = _micro + float(_slc.get("micro_potential_coeff", 0.30)) \
                             * (np.asarray(snow_potential_tile, np.float32) * 2.0 - 1.0)
                snow_px &= ((_t + 0.5 * _micro) > 0.5)

                # S89 EDGE STROKE: on TOP of the wide patchy band, hug the actual
                # snow/rock boundary with a thin (~2-3 block) ring of INTENSIFIED
                # salt-and-pepper so the fade-LINE itself is maximally ragged
                # (user ref: Building-101 mountain — jaggedness reads at the
                # border specifically). Symmetric: bite bare-rock flecks INTO the
                # snow edge, push snow flecks OUT onto candidate (gap==7) cells the
                # band just cut. Amp fades to 0 at the stroke edge. Restricting the
                # "grow" side to _snow_cand keeps it physical — no snow appears on
                # non-snow-source cells or in warm valleys.
                _stroke = float(_slc.get("edge_stroke_blocks", 2.0))
                _stroke_amp = float(_slc.get("edge_stroke_amp", 0.45))
                if _stroke >= 1.0 and _stroke_amp > 0.0 and snow_px.any() \
                        and not snow_px.all():
                    from scipy.ndimage import distance_transform_edt as _edt_stroke
                    _d_in = _edt_stroke(snow_px).astype(np.float32)
                    _d_out = _edt_stroke(~snow_px).astype(np.float32)
                    _edge_d = np.where(snow_px, _d_in, _d_out)   # ~1 at boundary
                    _ring = _edge_d <= _stroke
                    if _ring.any():
                        _amp = (_stroke_amp
                                * np.clip(1.0 - (_edge_d - 1.0) / _stroke, 0.0, 1.0))
                        _ergn = np.random.default_rng(
                            tile_x * 48271 ^ tile_y * 31337 ^ 0x5E0E)
                        _ecoin = _ergn.random((H, W)).astype(np.float32)
                        _bite = snow_px & _ring & (_ecoin < _amp)
                        _grow = (~snow_px) & _snow_cand & _ring & (_ecoin < _amp)
                        snow_px[_bite] = False
                        snow_px[_grow] = True
                        del (_ergn, _ecoin, _amp, _d_in, _d_out, _edge_d,
                             _ring, _bite, _grow)
                # S89 walk3: SNOW OVER ROCK + flat crests (measured, gated). Above
                # the per-biome line, GENTLE cells (rock or bare biome surface)
                # that pass the SAME patchy survival become snow too -- high flat
                # crests stop showing bare dirt and gentle rock gets blanketed,
                # while the slope cap + convex/aspect survival keep steep faces and
                # scoured ridges BARE (never 100% coverage).
                if cfg.get("snow_lines", {}).get("override_rock", True):
                    _or_slope = float(_slc.get("override_rock_max_slope_deg", 38.0))
                    _or_cand = ((surface_y >= _eff_line)
                                & (_gap != 4) & (_gap != 8) & (_gap != 9)
                                & (surface_y > 63))
                    if cliff_deg is not None:
                        _or_cand = _or_cand & (cliff_deg < _or_slope)
                    snow_px = snow_px | (_or_cand & ((_t + 0.5 * _micro) > 0.5))
                    del _or_cand
                del _snow_cand
            if snow_px.any():
                surface_blocks[snow_px] = "snow_block"
                # S84: powder_snow generation removed — was placed in concavities
                # but powder_snow is a hazard ("pest block") that traps players.
                # All snow now renders as snow_block.

                # S89 walk3: SNOW DRIFT FILL — pack snow into the eroded gully/
                # cirque hollows so the bowl reads as a deep SMOOTH snowfield
                # (ref image) while convex ridges stay bare rock. Raise surface_y
                # in concave snow cells by the local concavity (capped), measured
                # from the ERODED surface so the drifts follow the NEW gullies.
                # This gives the snow actual volume/amplitude and smooths the
                # couloir/bowl floors flush. Runs in decorate so it persists via
                # _post_decorate_y; convex cells (concavity<=0) are untouched.
                _df_max = float(_slc.get("drift_fill_blocks", 0.0))
                if _df_max > 0.0:
                    from scipy.ndimage import gaussian_filter as _gf_df
                    _syf_d = surface_y.astype(np.float32)
                    _concav = (_gf_df(_syf_d, float(_slc.get("drift_fill_sigma", 7.0)))
                               - _syf_d)                      # >0 in hollows
                    _fill = np.clip(_concav, 0.0, _df_max)
                    _dcells = snow_px & (_fill >= 1.0)
                    if _dcells.any():
                        from core.column_generator import MC_Y_MAX as _MCMAX_D
                        _raised = np.clip(_syf_d + _fill, None,
                                          float(_MCMAX_D - 1))
                        surface_y[_dcells] = np.round(
                            _raised[_dcells]).astype(surface_y.dtype)
                        surface_blocks[_dcells] = "snow_block"
                        del _syf_d, _concav, _fill, _dcells, _raised

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
    if _above.any() and not _overlay_off(cfg, "stone_fade"):
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
            _groups = _litho_cfg.get("groups", {})
            _DEFAULT_STONE_PAL = ["stone", "andesite", "granite", "diorite"]

            # Walk #4d: PAINTED lithology.tif is SOLE source of truth.
            # No biome fallback.  gid=0 + no mask = _DEFAULT_STONE_PAL.
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

            def _fade_paint(_bm, _pal):
                _n = len(_pal)
                for _i, _blk in enumerate(_pal):
                    _lo = _i / _n; _hi = (_i + 1) / _n
                    _band = _bm & (_scatter_pal >= _lo) & (_scatter_pal < _hi)
                    if _band.any():
                        surface_blocks[_band] = _blk
                subsurface_blocks[_bm] = _pal[0]

            if _litho_at_res_fade is not None:
                for _gid_v in np.unique(_litho_at_res_fade[_fade_mask]):
                    _gid_v = int(_gid_v)
                    _bm_l = _fade_mask & (_litho_at_res_fade == _gid_v)
                    if not _bm_l.any():
                        continue
                    _pal = _gid_to_pal_fade.get(_gid_v, _DEFAULT_STONE_PAL)
                    _fade_paint(_bm_l, _pal)
            else:
                _fade_paint(_fade_mask, _DEFAULT_STONE_PAL)

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
    # S89 depth-snow: potential-driven depth (caps + drifts + clean carpet).
    # Owns ALL snow when enabled; returns False (disabled / mask missing) to
    # fall back to the legacy 95%-dappled snow[layers=1] carpet.
    _depth_ran = False
    if snow_potential_tile is not None:
        _depth_ran = _apply_depth_snow(
            surface_blocks, ground_cover, biome_grid,
            snow_potential_tile,
            gap_mask=(eco_grads.gap_mask if (eco_grads is not None
                      and hasattr(eco_grads, "gap_mask")) else None),
            cliff_deg=cliff_deg,
            cfg=cfg,
            surface_y=surface_y,
            tile_x=tile_x,
            tile_y=tile_y,
            north_factor=(eco_grads.north_factor if (eco_grads is not None
                          and hasattr(eco_grads, "north_factor")) else None),
        )
    # In gully_only mode the depth pass only adds high-gully fingers, so the
    # original Gaea snow_carpet (SBT/FF/AT/BA dappled layers) still runs too.
    _gully_mode = bool(cfg.get("snow_physics", {})
                          .get("depth", {}).get("gully_only", False))
    if (not _depth_ran) or _gully_mode:
        _apply_snow_carpet(
            surface_blocks, ground_cover, biome_grid, cfg, tile_x, tile_y,
            cliff_deg=cliff_deg,  # S88: slope cap (skip snow on steep faces)
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

    # S87 walk #3 (36,15): ecotone block-swap uses RANDOM-SAMPLE.
    # Nearest-pixel sampling produced LONG LINEAR STRIPES of materials at
    # transition zones (e.g. podzol stripe across a KARST<->BT boundary).
    # Same root cause as the GC strip bug: nearest-pixel collapses many
    # swap pixels onto a small linear set of neighbor cells along the
    # boundary - those cells happen to share blocks and produce a stripe
    # parallel to the boundary.  Random sample restores per-pixel variation.
    # S89 fix (36,15 "fade band on the karst forest floor"): the ecotone
    # sample SOURCE must exclude gap-driven feature pixels, not just the swap
    # TARGET (line ~4308). Previously the sample pool was the WHOLE neighbour
    # biome — including its rock faces (gap==5, painted with rock_layers tiers
    # + wash). At any biome boundary touching a rock massif, the wide
    # (width_px=100) ramp random-sampled cliff stone/concrete blocks and copied
    # them onto the neighbouring forest/steppe floor up to 100 blocks out,
    # reading as a scattered "band" of andesite/diorite/cobblestone/calcite/
    # concrete-powder on flat ground. Restrict sampling to the neighbour
    # biome's NATURAL soil/vegetation surface: drop rock(5)/alpine(6)/snow(7)/
    # dune(8)/beach(9) — the same gradient-driven gaps excluded as targets.
    _sampleable = np.ones((H, W), dtype=bool)
    if gap_mask is not None:
        _sampleable &= (
            (gap_mask != 5) & (gap_mask != 6) & (gap_mask != 7)
            & (gap_mask != 8) & (gap_mask != 9)
        )

    nb_at_swap = neighbour_biome[swap_r, swap_c]
    for bname in biome_names:
        bname_mask = nb_at_swap == bname
        if not bname_mask.any():
            continue

        _biome_mask = (biome_grid == bname) & _sampleable
        biome_pixels_r, biome_pixels_c = np.where(_biome_mask)
        if len(biome_pixels_r) == 0:
            # Neighbour biome is entirely gap-driven here (all rock/snow/etc).
            # Fall back to its unfiltered pixels rather than skip the swap.
            biome_pixels_r, biome_pixels_c = np.where(biome_grid == bname)
        if len(biome_pixels_r) == 0:
            continue  # neighbor biome not present in this tile padded region

        target_r = swap_r[bname_mask]
        target_c = swap_c[bname_mask]

        n_swap = int(bname_mask.sum())
        sample_idx = rng.integers(0, len(biome_pixels_r), size=n_swap)
        sampled_r = biome_pixels_r[sample_idx]
        sampled_c = biome_pixels_c[sample_idx]

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
    # S87 (walk #3): floodplain pixels (gap==4) opt OUT of GC ecotone swap.
    # Floodplain has its own grass/mud palette via gap==4 surface decoration;
    # swapping to neighbour forest GC produced "forest groundcover in
    # floodplain" report at (26,10).
    if gap_mask is not None:
        do_swap_grid = do_swap_grid & (gap_mask != 4)
    if not do_swap_grid.any():
        return

    swap_r, swap_c = np.where(do_swap_grid)
    if len(swap_r) == 0:
        return

    # S87 (walk #3): RANDOM-SAMPLE for GC swap (not nearest-pixel).
    # 1F-full nearest-pixel sampling collapsed many swap pixels onto a small
    # set of densely-vegetated neighbor cells near the boundary, producing
    # "100% vegetation strips" at transition zones (user reported 26,10 /
    # 80,50 / 28,7 / 59,44).  Random sampling restores the natural per-pixel
    # variation in GC density across the swap zone.  Surface blocks still
    # use nearest-pixel (coherent palettes there look good).
    nb_at_swap = neighbour_biome[swap_r, swap_c]
    for bname in biome_names:
        bname_mask = nb_at_swap == bname
        if not bname_mask.any():
            continue
        biome_mask = biome_grid == bname
        biome_pixels_r, biome_pixels_c = np.where(biome_mask)
        if len(biome_pixels_r) == 0:
            continue

        target_r = swap_r[bname_mask]
        target_c = swap_c[bname_mask]

        n_swap = int(bname_mask.sum())
        sample_idx = rng.integers(0, len(biome_pixels_r), size=n_swap)
        sampled_r = biome_pixels_r[sample_idx]
        sampled_c = biome_pixels_c[sample_idx]

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