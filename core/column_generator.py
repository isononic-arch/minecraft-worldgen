"""
column_generator.py — Vandir Pipeline Step 6
=============================================
Generates the full vertical block column for every pixel in a tile.

Responsibilities:
  - Height remap (Gaea 16-bit → MC Y via locked spline)
  - Bedrock / stone / subsurface / surface block fill
  - Mandatory water fill for sub-sea-level terrain
  - Biome-driven surface + subsurface block selection (mask + noise)
  - Slope-based grass/stone mixing zone
  - Snow cap with slope-dependent variation (seed 42004)
  - Shoreline / beach transitions
  - Cliff face banding on steep tall columns
  - Underwater floor graduation (sand → gravel → stone by depth)
  - Sand dune geometry (SAND_DUNE_DESERT only)

Does NOT handle:
  - River/lake carving  → Step 6a  (river_pass.py)
  - Surface decoration  → Step 7
  - Schematic placement → Step 8

All thresholds loaded from config/thresholds.json — never hardcoded.

Column layout (locked):
  Y = MC_Y_MIN (-64)              → bedrock
  Y = MC_Y_MIN+1 .. surface_Y-3  → stone (with cliff banding on steep columns)
  Y = surface_Y-2 .. surface_Y-1 → subsurface block (biome-dependent)
  Y = surface_Y                   → surface block (biome + mask + noise)
  Y = surface_Y+1 .. SEA_LEVEL   → water  (MANDATORY if surface_Y < SEA_LEVEL)
  Y > SEA_LEVEL                  → air
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import NamedTuple

import numpy as np
from opensimplex import OpenSimplex
from scipy.ndimage import distance_transform_edt, binary_dilation

# ── Constants (locked) ────────────────────────────────────────────────────────
MC_Y_MIN   = -64
MC_Y_MAX   = 448
SEA_LEVEL  = 63
BEDROCK_Y  = MC_Y_MIN  # -64


# ── Block palette registry — all 27 biomes ────────────────────────────────────
# Format: list of (surface, subsurface, condition)
# Conditions: "base" | "moisture" | "erosion" | "noise" | "altitude"
# Priority order: altitude > erosion > moisture > noise > base
BIOME_BLOCK_PALETTES: dict[str, list[tuple[str, str, str]]] = {
    "COASTAL_HEATH": [
        ("grass_block", "stone",       "base"),
        ("gravel",      "stone",       "erosion"),
        ("stone",       "stone",       "noise"),
    ],
    "TEMPERATE_RAINFOREST": [
        ("podzol",      "coarse_dirt", "base"),
        ("podzol",      "mud",         "moisture"),
        ("coarse_dirt", "coarse_dirt", "erosion"),
        ("grass_block", "dirt",        "noise"),
    ],
    "BOREAL_TAIGA": [
        ("grass_block", "dirt",        "base"),
        ("podzol",      "coarse_dirt", "moisture"),
        ("gravel",      "stone",       "erosion"),
        ("snow_block",  "packed_ice",  "altitude"),
    ],
    "SNOWY_BOREAL_TAIGA": [
        ("snow_block",  "packed_ice",  "base"),
        ("powder_snow", "packed_ice",  "noise"),
    ],
    "ALPINE_MEADOW": [
        ("grass_block", "dirt",        "base"),
        ("gravel",      "stone",       "erosion"),
        ("stone",       "stone",       "noise"),
    ],
    "ARCTIC_TUNDRA": [
        ("snow_block",  "packed_ice",  "base"),
        ("stone",       "stone",       "erosion"),
        ("gravel",      "stone",       "noise"),
    ],
    "FROZEN_FLATS": [
        ("snow_block",  "dirt",        "base"),
        ("ice",         "dirt",        "moisture"),
        ("powder_snow", "dirt",        "noise"),
    ],
    "TEMPERATE_DECIDUOUS": [
        ("grass_block", "dirt",        "base"),
        ("coarse_dirt", "dirt",        "erosion"),
        ("podzol",      "coarse_dirt", "noise"),
    ],
    "RAINFOREST_COAST": [
        ("podzol",      "coarse_dirt", "base"),
        ("mud",         "mud",         "moisture"),
        ("coarse_dirt", "coarse_dirt", "erosion"),
    ],
    "RIPARIAN_WOODLAND": [
        ("mud",         "mud",         "base"),
        ("grass_block", "mud",         "noise"),
        ("clay",        "mud",         "moisture"),
    ],
    "DRY_OAK_SAVANNA": [
        ("grass_block", "dirt",        "base"),
        ("coarse_dirt", "dirt",        "erosion"),
        ("sand",        "sandstone",   "noise"),
    ],
    "KARST_BARRENS": [
        ("gravel",      "stone",       "base"),
        ("stone",       "stone",       "erosion"),
        ("coarse_dirt", "stone",       "noise"),
    ],
    "BIRCH_FOREST": [
        ("grass_block", "dirt",        "base"),
        ("coarse_dirt", "dirt",        "noise"),
    ],
    "EASTERN_TEMPERATE_COAST": [
        ("gravel",      "stone",       "base"),
        ("sand",        "sandstone",   "noise"),
        ("stone",       "stone",       "erosion"),
    ],
    "MIXED_FOREST": [
        ("grass_block", "dirt",        "base"),
        ("coarse_dirt", "dirt",        "erosion"),
        ("gravel",      "stone",       "noise"),
    ],
    "CONTINENTAL_STEPPE": [
        ("grass_block", "dirt",        "base"),
        ("coarse_dirt", "dirt",        "erosion"),
        ("gravel",      "dirt",        "noise"),
    ],
    "DRY_PINE_BARRENS": [
        ("coarse_dirt", "terracotta",  "base"),
        ("gravel",      "stone",       "erosion"),
        ("sand",        "sandstone",   "noise"),
    ],
    "SCRUBBY_HEATHLAND": [
        ("gravel",      "stone",       "base"),
        ("grass_block", "dirt",        "noise"),
        ("stone",       "stone",       "erosion"),
    ],
    "LUSH_RAINFOREST_COAST": [
        ("grass_block", "dirt",        "base"),
        ("podzol",      "coarse_dirt", "moisture"),
        ("mud",         "mud",         "erosion"),
    ],
    "SAND_DUNE_DESERT": [
        ("sand",        "sandstone",   "base"),
    ],
    "DESERT_STEPPE_TRANSITION": [
        ("gravel",      "stone",       "base"),
        ("coarse_dirt", "dirt",        "noise"),
        ("sand",        "sandstone",   "erosion"),
    ],
    "SEMI_ARID_SHRUBLAND": [
        ("coarse_dirt", "dirt",        "base"),
        ("sand",        "sandstone",   "erosion"),
        ("gravel",      "stone",       "noise"),
    ],
    "DRY_WOODLAND_MAQUIS": [
        ("coarse_dirt", "dirt",        "base"),
        ("gravel",      "stone",       "erosion"),
        ("sand",        "sandstone",   "noise"),
    ],
    "TIDAL_JUNGLE_FRINGE": [
        ("mud",         "mud",         "base"),
        ("grass_block", "dirt",        "noise"),
    ],
    "MANGROVE_COAST": [
        ("mud",         "mud",         "base"),
        ("clay",        "mud",         "moisture"),
    ],
    "FRESHWATER_FEN": [
        ("mud",         "mud",         "base"),
        ("grass_block", "mud",         "noise"),
        ("clay",        "mud",         "moisture"),
    ],
}

# Rocky biomes that get gravel beaches instead of sand
ROCKY_BEACH_BIOMES = {"COASTAL_HEATH", "KARST_BARRENS", "SCRUBBY_HEATHLAND",
                      "EASTERN_TEMPERATE_COAST"}

# Desert biomes (use sandstone as stone substitute)
DESERT_BIOMES = {"SAND_DUNE_DESERT", "DESERT_STEPPE_TRANSITION",
                 "SEMI_ARID_SHRUBLAND", "DRY_WOODLAND_MAQUIS"}


# ── Height remap ───────────────────────────────────────────────────────────────

def gaea_to_mc_y(height_16bit: int) -> int:
    """Map a single 16-bit Gaea height value to MC Y coordinate."""
    return int(_LUT[np.clip(height_16bit, 0, 65535)])


# ── Noise helpers ─────────────────────────────────────────────────────────────

def _noise_01(gen: OpenSimplex, x: float, y: float) -> float:
    """Return OpenSimplex noise remapped to [0, 1]."""
    return (gen.noise2(x, y) + 1.0) * 0.5


def _fbm(gen: OpenSimplex, x: float, y: float, octaves: int = 4) -> float:
    """Simple fBm — sum of octaves with halving amplitude."""
    val, amp, freq = 0.0, 1.0, 1.0
    for _ in range(octaves):
        val  += gen.noise2(x * freq, y * freq) * amp
        amp  *= 0.5
        freq *= 2.0
    # Remap to [0,1]
    return (val / (2.0 - 2.0 ** (1 - octaves)) + 1.0) * 0.5


# ── Surface block resolution ──────────────────────────────────────────────────

def resolve_surface_blocks(
    biome:             str,
    erosion_norm:      float,   # [0,1]
    moisture_norm:     float,   # [0,1]
    altitude_override: bool,
    px: int, py: int,
    mix_gen:           OpenSimplex,
    cfg:               dict,
) -> tuple[str, str]:
    """
    Return (surface_block, subsurface_block) for this pixel.
    Priority: altitude > erosion > moisture > noise > base
    """
    palette = BIOME_BLOCK_PALETTES.get(biome)
    if palette is None:
        return ("grass_block", "dirt")

    if altitude_override:
        for surf, sub, cond in palette:
            if cond == "altitude":
                return surf, sub

    if erosion_norm > cfg["erosion_threshold"]:
        for surf, sub, cond in palette:
            if cond == "erosion":
                return surf, sub

    if moisture_norm > cfg["moisture_threshold"]:
        for surf, sub, cond in palette:
            if cond == "moisture":
                return surf, sub

    n = _noise_01(mix_gen, px / cfg["noise_scale"], py / cfg["noise_scale"])
    if n > cfg["noise_threshold"]:
        for surf, sub, cond in palette:
            if cond == "noise":
                return surf, sub

    for surf, sub, cond in palette:
        if cond == "base":
            return surf, sub

    return ("grass_block", "dirt")


# ── Slope-based surface override ──────────────────────────────────────────────

def apply_slope_surface(
    biome:       str,
    surface:     str,
    slope_norm:  float,   # [0,1]
    px: int, py: int,
    slope_gen:   OpenSimplex,
    cfg:         dict,
) -> str:
    """
    Override surface block based on slope.
    steep  → stone
    gentle → keep biome surface
    mix    → noise-blended
    """
    stone = "sandstone" if biome in DESERT_BIOMES else "stone"

    if slope_norm >= cfg["stone_hard_threshold"]:
        return stone
    if slope_norm <= cfg["grass_hard_threshold"]:
        return surface

    scale = cfg["mix_zone_noise_scale"]
    n = _noise_01(slope_gen, px / scale, py / scale)
    t = (slope_norm - cfg["grass_hard_threshold"]) / (
        cfg["stone_hard_threshold"] - cfg["grass_hard_threshold"]
    )
    return stone if n < t else surface


# ── Snow cap ──────────────────────────────────────────────────────────────────

def apply_snow_cap(
    surface_y: int,
    slope_norm: float,
    px: int, py: int,
    snow_gen:   OpenSimplex,
    cfg:        dict,
) -> str | None:
    """
    Return "snow" to place a snow layer on top, or None.
    Snow line varies with slope and noise (±25 Y).
    """
    base_line = cfg["snow_line_y"]
    scale     = cfg["snow_noise_scale"]
    jitter    = cfg["snow_noise_amplitude"]
    slope_adj = cfg["snow_slope_offset"]  # steeper = higher snow line

    n = _noise_01(snow_gen, px / scale, py / scale)
    snow_y = base_line + int((n - 0.5) * 2 * jitter) + int(slope_norm * slope_adj)

    if surface_y >= snow_y:
        return "snow"
    return None


# ── Shoreline / beach ─────────────────────────────────────────────────────────

def shoreline_surface(
    biome:     str,
    surface_y: int,
    dist_to_shore: int,   # blocks above/below sea level (positive = above)
    slope_norm: float,
    px: int, py: int,
    shore_gen: OpenSimplex,
    cfg:       dict,
) -> tuple[str, str] | None:
    """
    Returns overridden (surface, subsurface) for shoreline pixels, or None.
    """
    beach_w = cfg["sand_beach_width_blocks"]
    if dist_to_shore < 0 or dist_to_shore > beach_w:
        return None

    rocky = (biome in ROCKY_BEACH_BIOMES
             or slope_norm > 0.4
             or _noise_01(shore_gen, px / cfg["noise_scale"],
                          py / cfg["noise_scale"]) < cfg["gravel_chance"])

    if rocky:
        return ("gravel", "stone")

    # Sand beach — closer to water = pure sand, further = mixed
    if dist_to_shore <= 2:
        return ("sand", "sandstone")
    else:
        n = _noise_01(shore_gen, px / cfg["noise_scale"], py / cfg["noise_scale"])
        return ("gravel", "stone") if n < 0.25 else ("sand", "sandstone")


# ── Cliff banding ─────────────────────────────────────────────────────────────

# Per-biome cliff stone variants (Norterre-inspired geological variety)
_BIOME_CLIFF_VARIANTS: dict[str, list[str]] = {
    # Forest biomes — mossy stone exposure
    "TEMPERATE_RAINFOREST":    ["stone", "mossy_cobblestone", "andesite", "stone", "cobblestone"],
    "LUSH_RAINFOREST_COAST":   ["stone", "mossy_cobblestone", "andesite", "stone", "cobblestone"],
    "RAINFOREST_COAST":        ["stone", "mossy_cobblestone", "andesite", "stone", "cobblestone"],
    "TEMPERATE_DECIDUOUS":     ["stone", "andesite", "cobblestone", "stone", "mossy_cobblestone"],
    "MIXED_FOREST":            ["stone", "andesite", "cobblestone", "stone", "mossy_cobblestone"],
    "BOREAL_TAIGA":            ["stone", "andesite", "diorite", "stone", "cobblestone"],
    "BIRCH_FOREST":            ["stone", "diorite", "andesite", "stone", "cobblestone"],
    "RIPARIAN_WOODLAND":       ["stone", "mossy_cobblestone", "cobblestone", "stone", "andesite"],
    # Alpine / Arctic — stark geological exposure
    "ALPINE_MEADOW":           ["stone", "andesite", "diorite", "granite", "stone", "calcite"],
    "ARCTIC_TUNDRA":           ["stone", "andesite", "diorite", "stone", "gravel"],
    "FROZEN_FLATS":            ["stone", "packed_ice", "andesite", "stone", "gravel"],
    "SNOWY_BOREAL_TAIGA":      ["stone", "diorite", "andesite", "stone", "gravel"],
    # Arid / Desert — sandstone and terracotta
    "SAND_DUNE_DESERT":        ["sandstone", "smooth_sandstone", "sandstone", "red_sandstone", "sandstone"],
    "DRY_OAK_SAVANNA":         ["sandstone", "terracotta", "orange_terracotta", "sandstone", "red_sandstone"],
    "DESERT_STEPPE_TRANSITION":["sandstone", "stone", "terracotta", "sandstone", "andesite"],
    "SEMI_ARID_SHRUBLAND":     ["stone", "andesite", "sandstone", "stone", "granite"],
    "DRY_WOODLAND_MAQUIS":     ["stone", "terracotta", "andesite", "stone", "granite"],
    "DRY_PINE_BARRENS":        ["stone", "granite", "andesite", "stone", "sandstone"],
    # Karst — calcite/dripstone
    "KARST_BARRENS":           ["stone", "calcite", "dripstone_block", "stone", "cobblestone"],
    # Steppe — granite base
    "CONTINENTAL_STEPPE":      ["stone", "granite", "andesite", "stone", "cobblestone"],
    "SCRUBBY_HEATHLAND":       ["stone", "andesite", "tuff", "stone", "cobblestone"],
    # Coastal
    "COASTAL_HEATH":           ["stone", "andesite", "cobblestone", "stone", "gravel"],
    "EASTERN_TEMPERATE_COAST": ["stone", "andesite", "cobblestone", "stone", "gravel"],
    # Tropical / Wetland
    "TIDAL_JUNGLE_FRINGE":     ["stone", "mossy_cobblestone", "andesite", "stone", "cobblestone"],
    "MANGROVE_COAST":          ["stone", "clay", "andesite", "stone", "cobblestone"],
    "FRESHWATER_FEN":          ["stone", "clay", "andesite", "stone", "cobblestone"],
}
_DEFAULT_CLIFF_VARIANTS = ["stone", "gravel", "tuff", "cobblestone", "stone"]

def cliff_band_block(y: int, px: int, py: int, cliff_gen: OpenSimplex,
                     cfg: dict, biome: str = "") -> str:
    """Return the cliff band block for a given Y and XZ position.

    Uses per-biome cliff stone variants for geological variety.
    Two-noise banding for irregular, natural-looking horizontal layers.
    """
    variants = _BIOME_CLIFF_VARIANTS.get(biome, _DEFAULT_CLIFF_VARIANTS)
    scale_y  = cfg["band_scale_y"]
    scale_xz = cfg["band_noise_scale_xz"]
    n_xz  = _noise_01(cliff_gen, px / scale_xz, py / scale_xz)
    # Second noise octave at different frequency for irregularity
    n_xz2 = _noise_01(cliff_gen, px / (scale_xz * 2.3) + 500, py / (scale_xz * 2.3) + 500)
    band_index = int((y / scale_y + n_xz * 0.6 + n_xz2 * 0.3) % len(variants))
    return variants[band_index]


# ── Underwater floor ──────────────────────────────────────────────────────────

def underwater_floor_block(depth_below_sea: int, cfg: dict) -> str:
    """Return the block for ocean floor at given depth below sea level."""
    if depth_below_sea <= cfg["sand_max_depth"]:
        return "sand"
    if depth_below_sea <= cfg["gravel_max_depth"]:
        return "gravel"
    return "stone"


# ── Dune height offset ────────────────────────────────────────────────────────

def dune_height_offset(
    px: int, py: int,
    dune_gen_a: OpenSimplex,
    dune_gen_b: OpenSimplex,
    cfg:        dict,
) -> int:
    """Additional Y to add to base terrain for sand dune geometry."""
    n_a = dune_gen_a.noise2(px / cfg["scale_a"], py / (cfg["scale_a"] * 2.5))
    n_a = (n_a + 1.0) * 0.5
    n_b = dune_gen_b.noise2(px / cfg["scale_b"], py / cfg["scale_b"])
    n_b = (n_b + 1.0) * 0.5
    combined = n_a * cfg["amplitude_a"] + n_b * cfg["amplitude_b"]
    return int(max(cfg["min_height_add"], min(cfg["max_height_add"], combined)))


# ── Main column generator ─────────────────────────────────────────────────────

class ColumnResult(NamedTuple):
    """Output of generate_column — consumed by chunk_writer."""
    surface_y:    int          # final surface Y after all adjustments
    blocks:       dict[int, str]  # Y → block_id, sparse (only non-air/non-stone)
    snow_layer:   bool         # place snow layer at surface_y + 1
    biome_id:     str          # MC biome string


def generate_column(
    px:         int,    # world pixel X (0–49999)
    py:         int,    # world pixel Z (0–49999)
    height_16:  int,    # raw Gaea height value (16-bit, inverted polarity)
    slope_norm: float,  # normalised slope [0,1]
    erosion_norm: float,
    flow_norm:  float,
    deposits_norm: float,
    is_shore:   bool,   # pixel within shore distance of sea level
    biome:      str,    # resolved biome name e.g. "TEMPERATE_RAINFOREST"
    mc_biome:   str,    # MC biome ID string e.g. "old_growth_spruce_taiga"
    noise_gens: dict[str, OpenSimplex],
    cfg:        dict,   # full thresholds.json content
) -> ColumnResult:
    """
    Generate all blocks for a single (px, py) column.

    Returns a ColumnResult with sparse block dict and metadata.
    The chunk_writer expands this into a full ChunkSection NumPy array.
    """
    # ── 1. Height remap ───────────────────────────────────────────────────────
    # Height mask is INVERTED: low raw value = high terrain, high raw = ocean.
    # Spline is calibrated for direct raw input (no inversion needed).
    surface_y = gaea_to_mc_y(height_16)

    # ── 2. Dune geometry (SAND_DUNE_DESERT only) ──────────────────────────────
    dune_add = 0
    if biome == "SAND_DUNE_DESERT":
        dune_add = dune_height_offset(
            px, py,
            noise_gens["dune_a"], noise_gens["dune_b"],
            cfg["sand_dunes"],
        )
        surface_y += dune_add

    surface_y = max(MC_Y_MIN + 4, min(MC_Y_MAX - 1, surface_y))
    is_ocean = surface_y <= SEA_LEVEL

    # ── 3. Surface + subsurface block selection ───────────────────────────────
    altitude_override = surface_y >= cfg["snow_cap"]["snow_line_y"]
    moisture_norm = flow_norm  # flow is moisture proxy

    surface_blk, subsurface_blk = resolve_surface_blocks(
        biome, erosion_norm, moisture_norm, altitude_override,
        px, py, noise_gens["slope_mix"], cfg["block_mixing"],
    )

    # ── 4. Slope override ─────────────────────────────────────────────────────
    if not is_ocean:
        surface_blk = apply_slope_surface(
            biome, surface_blk, slope_norm,
            px, py, noise_gens["slope_mix"], cfg["slope_surface"],
        )

    # ── 5. Shoreline override ─────────────────────────────────────────────────
    if is_shore and not is_ocean:
        dist_above = surface_y - SEA_LEVEL
        shore_result = shoreline_surface(
            biome, surface_y, dist_above, slope_norm,
            px, py, noise_gens["slope_mix"], cfg["shoreline"],
        )
        if shore_result:
            surface_blk, subsurface_blk = shore_result

    # ── 6. Build block dict (sparse — only non-stone non-air entries) ─────────
    blocks: dict[int, str] = {}

    is_cliff = (slope_norm >= cfg["slope_surface"]["stone_hard_threshold"]
                and (surface_y - BEDROCK_Y) > 80)

    # Bedrock
    blocks[BEDROCK_Y] = "bedrock"

    # Stone interior — only emit if it's cliff-banded (otherwise chunk_writer defaults)
    if is_cliff and cfg.get("cliff_banding", {}).get("enabled", False):
        cliff_gen = noise_gens["slope_mix"]  # reuse slope_mix gen for XZ jitter
        for y in range(BEDROCK_Y + 1, surface_y - 1):
            blocks[y] = cliff_band_block(y, px, py, cliff_gen, cfg["cliff_banding"],
                                           biome=biome_id)
    # else: stone is the default fill — chunk_writer handles it, no entries needed

    # Subsurface (2 layers)
    if surface_y - 2 > BEDROCK_Y:
        blocks[surface_y - 2] = subsurface_blk
    if surface_y - 1 > BEDROCK_Y:
        blocks[surface_y - 1] = subsurface_blk

    # Surface block
    blocks[surface_y] = surface_blk

    # ── 7. Underwater fill ────────────────────────────────────────────────────
    if is_ocean:
        # Ocean floor material (replaces surface block)
        depth_below = SEA_LEVEL - surface_y
        floor_blk = underwater_floor_block(depth_below, cfg["ocean_floor"])
        blocks[surface_y] = floor_blk
        if surface_y - 1 > BEDROCK_Y:
            blocks[surface_y - 1] = floor_blk if depth_below <= 2 else subsurface_blk

        # Water column from surface+1 to sea level
        for y in range(surface_y + 1, SEA_LEVEL + 1):
            blocks[y] = "water"

        # Near-shore underwater sand
        if is_shore and depth_below <= cfg["shoreline"]["sand_underwater_depth"]:
            blocks[surface_y] = "sand"
            if surface_y - 1 > BEDROCK_Y:
                blocks[surface_y - 1] = "sandstone"

    # ── 8. Snow cap ───────────────────────────────────────────────────────────
    snow_layer = False
    if not is_ocean and biome not in {"SNOWY_BOREAL_TAIGA", "ARCTIC_TUNDRA", "FROZEN_FLATS"}:
        snow = apply_snow_cap(
            surface_y, slope_norm,
            px, py, noise_gens["snow_line"], cfg["snow_cap"],
        )
        if snow:
            snow_layer = True

    # ── 9. Dune sand fill (below dune bump, above original terrain) ───────────
    if dune_add > 0:
        orig_y = surface_y - dune_add
        for y in range(orig_y + 1, surface_y):
            blocks[y] = "sand"

    return ColumnResult(
        surface_y  = surface_y,
        blocks     = blocks,
        snow_layer = snow_layer,
        biome_id   = mc_biome,
    )


# ── Tile-level batch processor ────────────────────────────────────────────────

def process_tile_columns(
    tile_height:   np.ndarray,    # (512, 512) uint16, Gaea height
    tile_slope:    np.ndarray,    # (512, 512) uint16
    tile_erosion:  np.ndarray,    # (512, 512) uint16
    tile_flow:     np.ndarray,    # (512, 512) uint16
    tile_deposits: np.ndarray,    # (512, 512) uint16
    tile_shore:    np.ndarray,    # (512, 512) bool
    tile_biomes:   np.ndarray,    # (512, 512) str — resolved biome names
    tile_mc_biomes: np.ndarray,   # (512, 512) str — MC biome IDs
    tile_origin_x: int,           # world pixel origin X
    tile_origin_y: int,           # world pixel origin Z
    noise_gens:    dict[str, OpenSimplex],
    cfg:           dict,
) -> list[list[ColumnResult]]:
    """
    Process all 512×512 columns in a tile.
    Returns a 2D list [row][col] of ColumnResult.

    Called by tile_streamer.py — this is the inner loop.
    Performance note: cliff banding is the expensive path (per-Y loop).
    Most columns (non-cliff) are very fast.
    """
    h, w = tile_height.shape
    results: list[list[ColumnResult]] = []

    # Normalise masks to [0,1] once for the tile
    slope_f    = tile_slope.astype(np.float32)    / 65535.0
    erosion_f  = tile_erosion.astype(np.float32)  / 65535.0
    flow_f     = tile_flow.astype(np.float32)     / 65535.0
    deposits_f = tile_deposits.astype(np.float32) / 65535.0

    for row in range(h):
        row_results: list[ColumnResult] = []
        for col in range(w):
            px = tile_origin_x + col
            py = tile_origin_y + row

            result = generate_column(
                px           = px,
                py           = py,
                height_16    = int(tile_height[row, col]),
                slope_norm   = float(slope_f[row, col]),
                erosion_norm = float(erosion_f[row, col]),
                flow_norm    = float(flow_f[row, col]),
                deposits_norm= float(deposits_f[row, col]),
                is_shore     = bool(tile_shore[row, col]),
                biome        = tile_biomes[row, col],
                mc_biome     = tile_mc_biomes[row, col],
                noise_gens   = noise_gens,
                cfg          = cfg,
            )
            row_results.append(result)
        results.append(row_results)

    return results


# ── thresholds.json defaults (used if file not present) ──────────────────────

DEFAULT_THRESHOLDS_STEP6 = {
    "block_mixing": {
        "erosion_threshold":  0.60,
        "moisture_threshold": 0.65,
        "noise_scale":        25,
        "noise_threshold":    0.68,
    },
    "slope_surface": {
        "stone_hard_threshold": 0.65,
        "grass_hard_threshold": 0.35,
        "mix_zone_noise_scale": 40,
    },
    "snow_cap": {
        "snow_line_y":         280,
        "snow_noise_scale":    80,
        "snow_noise_amplitude": 25,
        "snow_slope_offset":   30,
    },
    "shoreline": {
        "sand_beach_width_blocks":   4,
        "gravel_beach_width_blocks": 3,
        "sand_underwater_depth":     6,
        "gravel_chance":             0.25,
        "noise_scale":               30,
    },
    "cliff_banding": {
        "enabled":              True,
        "band_scale_y":         12,
        "band_noise_scale_xz":  20,
    },
    "slope_zones": {
        "full_grass_max_deg":    25,     # below this: 100% biome surface
        "transition_max_deg":    50,     # 25-50: mixed biome/stone scatter
        "full_cliff_min_deg":    50,     # above this: full cliff stone + banding
        "transition_stone_frac": 0.5,   # fraction of transition zone that becomes stone
    },
    "talus": {
        "enabled":              True,
        "dilate_px":            8,       # how far scree extends from cliff base
        "gravel_frac":          0.5,     # fraction gravel vs cobblestone
    },
    "shoreline_gradient": {
        "sand_height":          3,       # blocks above sea level: sand
        "gravel_height":        6,       # blocks above sea level: gravel/clay mix
        "transition_height":   10,       # blocks above sea level: coarse_dirt
    },
    "ocean_floor": {
        "sand_max_depth":   8,
        "gravel_max_depth": 20,
        "stone_below":      20,
    },
    "sand_dunes": {
        "scale_a":        80,
        "scale_b":        35,
        "amplitude_a":    10,
        "amplitude_b":     5,
        "min_height_add":  0,
        "max_height_add": 15,
    },
}


def load_cfg(thresholds_path: Path) -> dict:
    """Load thresholds.json, merging missing step-6 keys from defaults."""
    if thresholds_path.exists():
        with open(thresholds_path) as f:
            cfg = json.load(f)
    else:
        cfg = {}
    for key, val in DEFAULT_THRESHOLDS_STEP6.items():
        if key not in cfg:
            cfg[key] = val
    return cfg


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from opensimplex import OpenSimplex

    cfg = load_cfg(Path("config/thresholds.json"))

    gens = {
        "slope_mix": OpenSimplex(seed=42003),
        "snow_line": OpenSimplex(seed=42004),
        "dune_a":    OpenSimplex(seed=42005),
        "dune_b":    OpenSimplex(seed=42006),
    }

    test_cases = [
        # (height_16, slope, erosion, flow, is_shore, biome, mc_biome, label)
        (20000, 0.1,  0.3, 0.05, False, "TEMPERATE_RAINFOREST", "old_growth_spruce_taiga", "rainforest lowland"),
        (5000,  0.05, 0.2, 0.8,  True,  "FRESHWATER_FEN",       "swamp",                   "fen at shore"),
        (60000, 0.8,  0.6, 0.01, False, "ARCTIC_TUNDRA",        "frozen_peaks",             "high alpine cliff"),
        (16000, 0.02, 0.1, 0.1,  True,  "COASTAL_HEATH",        "windswept_hills",          "coastline"),
        (25000, 0.15, 0.2, 0.02, False, "SAND_DUNE_DESERT",     "desert",                   "desert dune"),
        (1000,  0.01, 0.05, 0.3, False, "MANGROVE_COAST",       "mangrove_swamp",           "deep ocean"),
    ]

    print("=" * 60)
    print("  Step 6 — column_generator.py smoke test")
    print("=" * 60)
    all_ok = True
    for h16, sl, er, fl, shore, biome, mc, label in test_cases:
        r = generate_column(
            px=1000, py=2000,
            height_16=h16, slope_norm=sl, erosion_norm=er,
            flow_norm=fl, deposits_norm=0.1,
            is_shore=shore, biome=biome, mc_biome=mc,
            noise_gens=gens, cfg=cfg,
        )
        # Validate invariants
        ok = True
        msgs = []
        if r.blocks.get(BEDROCK_Y) != "bedrock":
            ok = False; msgs.append("missing bedrock")
        # Water fill check
        if r.surface_y < SEA_LEVEL:
            for y in range(r.surface_y + 1, SEA_LEVEL + 1):
                if r.blocks.get(y) != "water":
                    ok = False; msgs.append(f"missing water at Y{y}"); break
        # Surface block present
        if r.surface_y not in r.blocks:
            ok = False; msgs.append("no surface block")

        status = "✓" if ok else "✗"
        print(f"  {status} {label:<30} surface_Y={r.surface_y:4d}  "
              f"surface={r.blocks.get(r.surface_y,'?'):<15} "
              f"snow={r.snow_layer}")
        if msgs:
            for m in msgs: print(f"      ERROR: {m}")
            all_ok = False

    print(f"\n  {'ALL PASSED' if all_ok else 'FAILURES — fix before Step 6a'}")
    print("=" * 60)
"""
process_tile_columns_v2.py
Vectorized replacement for the per-column loop in column_generator.py.

Drop-in patch: adds process_tile_columns_v2() to column_generator.py.
validate_test_tile.py and run_pipeline.py should call this instead of
process_tile_columns().

Returns the same list[list[ColumnResult]] structure so downstream code
(river_carver, chunk_writer) is unchanged.

Speedup: ~100-200x over the Python loop (seconds vs minutes per tile).
"""

import numpy as np

# These constants must match column_generator.py
MC_Y_MIN   = -64
MC_Y_MAX   =  448
SEA_LEVEL  =   63
BEDROCK_Y  =  -64
Y_RANGE    =  512   # MC_Y_MAX - MC_Y_MIN + 1  = 512... wait 448-(-64)+1=513
# Actually Y_RANGE = 448 - (-64) + 1 = 513, but chunk_writer uses 512
# Check column_generator constants:
_Y_RANGE   = MC_Y_MAX - MC_Y_MIN  # = 512  (exclusive top)


def _build_lut_vectorized() -> np.ndarray:
    """
    Build a 65536-entry LUT mapping raw 16-bit Gaea height → MC Y.
    Normal polarity (confirmed Session 13): HIGH raw = HIGH terrain.
    Breakpoints (ascending gaea → ascending mc_y):
        Gaea     0 → MC Y  -64  (ocean floor)
        Gaea 17050 → MC Y   63  (sea level — MUST equal SEA_LEVEL exactly)
        Gaea 45000 → MC Y  200
        Gaea 65496 → MC Y  448  (peak terrain)
    These values match config/thresholds.json terrain_spline and all display tools.
    """
    gaea_in  = np.array([0, 17050, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64,   63,   200,   448], dtype=np.float64)
    lut = np.interp(np.arange(65536, dtype=np.float64), gaea_in, mc_y_out)
    return np.clip(lut, MC_Y_MIN + 4, MC_Y_MAX - 1).astype(np.int16)


_LUT = _build_lut_vectorized()


# ---------------------------------------------------------------------------
# Per-biome surface block tables
# ---------------------------------------------------------------------------

# (surface_block, subsurface_block) base defaults per biome
_BIOME_BASE_BLOCKS: dict[str, tuple[str, str]] = {
    "COASTAL_HEATH":           ("grass_block",  "dirt"),
    "TEMPERATE_RAINFOREST":    ("podzol",        "coarse_dirt"),
    "BOREAL_TAIGA":            ("grass_block",  "dirt"),
    "SNOWY_BOREAL_TAIGA":      ("snow_block",   "dirt"),
    "ALPINE_MEADOW":           ("grass_block",  "dirt"),
    "ARCTIC_TUNDRA":           ("snow_block",   "packed_ice"),
    "FROZEN_FLATS":            ("ice",          "packed_ice"),
    "TEMPERATE_DECIDUOUS":     ("grass_block",  "dirt"),
    "RAINFOREST_COAST":        ("podzol",        "coarse_dirt"),
    "RIPARIAN_WOODLAND":       ("grass_block",  "mud"),
    "DRY_OAK_SAVANNA":         ("grass_block",  "dirt"),
    "KARST_BARRENS":           ("gravel",       "stone"),
    "BIRCH_FOREST":            ("grass_block",  "dirt"),
    "EASTERN_TEMPERATE_COAST": ("grass_block",  "dirt"),
    "MIXED_FOREST":            ("grass_block",  "dirt"),
    "CONTINENTAL_STEPPE":      ("grass_block",  "dirt"),
    "DRY_PINE_BARRENS":        ("coarse_dirt",  "terracotta"),
    "SCRUBBY_HEATHLAND":       ("gravel",       "stone"),
    "LUSH_RAINFOREST_COAST":   ("grass_block",  "dirt"),
    "SAND_DUNE_DESERT":        ("sand",         "sandstone"),
    "DESERT_STEPPE_TRANSITION":("sand",         "sandstone"),
    "SEMI_ARID_SHRUBLAND":     ("coarse_dirt",  "dirt"),
    "DRY_WOODLAND_MAQUIS":     ("coarse_dirt",  "dirt"),
    "TIDAL_JUNGLE_FRINGE":     ("mud",          "mud"),
    "MANGROVE_COAST":          ("mud",          "mud"),
    "FRESHWATER_FEN":          ("mud",          "mud"),
    "_OCEAN":                  ("sand",         "sandstone"),
    "":                        ("stone",        "stone"),
}
_DEFAULT_SURFACE = ("grass_block", "dirt")


def _fbm_array(gen, world_x: np.ndarray, world_z: np.ndarray,
               octaves: int = 4, scale: float = 1.0) -> np.ndarray:
    """
    Evaluate fBm noise over 2D coordinate arrays.
    Returns float32 array in [0, 1], same shape as world_x.
    """
    value = np.zeros(world_x.shape, dtype=np.float64)
    amplitude = 1.0
    frequency = 1.0 / scale
    max_val   = 0.0
    for _ in range(octaves):
        # OpenSimplex noise2 is scalar; we vectorize via fromiter
        flat_x = (world_x * frequency).ravel()
        flat_z = (world_z * frequency).ravel()
        n = np.array([gen.noise2(float(x), float(z))
                      for x, z in zip(flat_x, flat_z)],
                     dtype=np.float64).reshape(world_x.shape)
        value    += n * amplitude
        max_val  += amplitude
        amplitude *= 0.5
        frequency *= 2.0
    return ((value / max_val + 1.0) / 2.0).astype(np.float32)


# ---------------------------------------------------------------------------
# Per-biome surface block tables
# ---------------------------------------------------------------------------

# (surface_block, subsurface_block) base defaults per biome
_BIOME_BASE_BLOCKS: dict[str, tuple[str, str]] = {
    "COASTAL_HEATH":           ("grass_block",  "dirt"),
    "TEMPERATE_RAINFOREST":    ("podzol",        "coarse_dirt"),
    "BOREAL_TAIGA":            ("grass_block",  "dirt"),
    "SNOWY_BOREAL_TAIGA":      ("snow_block",   "dirt"),
    "ALPINE_MEADOW":           ("grass_block",  "dirt"),
    "ARCTIC_TUNDRA":           ("snow_block",   "packed_ice"),
    "FROZEN_FLATS":            ("ice",          "packed_ice"),
    "TEMPERATE_DECIDUOUS":     ("grass_block",  "dirt"),
    "RAINFOREST_COAST":        ("podzol",        "coarse_dirt"),
    "RIPARIAN_WOODLAND":       ("grass_block",  "mud"),
    "DRY_OAK_SAVANNA":         ("grass_block",  "dirt"),
    "KARST_BARRENS":           ("gravel",       "stone"),
    "BIRCH_FOREST":            ("grass_block",  "dirt"),
    "EASTERN_TEMPERATE_COAST": ("grass_block",  "dirt"),
    "MIXED_FOREST":            ("grass_block",  "dirt"),
    "CONTINENTAL_STEPPE":      ("grass_block",  "dirt"),
    "DRY_PINE_BARRENS":        ("coarse_dirt",  "terracotta"),
    "SCRUBBY_HEATHLAND":       ("gravel",       "stone"),
    "LUSH_RAINFOREST_COAST":   ("grass_block",  "dirt"),
    "SAND_DUNE_DESERT":        ("sand",         "sandstone"),
    "DESERT_STEPPE_TRANSITION":("sand",         "sandstone"),
    "SEMI_ARID_SHRUBLAND":     ("coarse_dirt",  "dirt"),
    "DRY_WOODLAND_MAQUIS":     ("coarse_dirt",  "dirt"),
    "TIDAL_JUNGLE_FRINGE":     ("mud",          "mud"),
    "MANGROVE_COAST":          ("mud",          "mud"),
    "FRESHWATER_FEN":          ("mud",          "mud"),
    "_OCEAN":                  ("sand",         "sandstone"),
    "":                        ("stone",        "stone"),
}
_DEFAULT_SURFACE = ("grass_block", "dirt")


def _fbm_array(gen, world_x: np.ndarray, world_z: np.ndarray,
               octaves: int = 4, scale: float = 1.0) -> np.ndarray:
    """
    Evaluate fBm noise over 2D coordinate arrays.
    Returns float32 array in [0, 1], same shape as world_x.
    """
    value = np.zeros(world_x.shape, dtype=np.float64)
    amplitude = 1.0
    frequency = 1.0 / scale
    max_val   = 0.0
    for _ in range(octaves):
        # OpenSimplex noise2 is scalar; we vectorize via fromiter
        flat_x = (world_x * frequency).ravel()
        flat_z = (world_z * frequency).ravel()
        n = np.array([gen.noise2(float(x), float(z))
                      for x, z in zip(flat_x, flat_z)],
                     dtype=np.float64).reshape(world_x.shape)
        value    += n * amplitude
        max_val  += amplitude
        amplitude *= 0.5
        frequency *= 2.0
    return ((value / max_val + 1.0) / 2.0).astype(np.float32)


# ---- Temporary diagnostic flags (Step 3: set to True to disable noise blobs) ----
_DEBUG_DISABLE_NOISE = False   # set True → zeroes all noise conditions for tile (32,2)


def process_tile_columns_v2(
    tile_height:    np.ndarray,   # (H, W) uint16
    tile_slope:     np.ndarray,   # (H, W) uint16
    tile_erosion:   np.ndarray,   # (H, W) uint16
    tile_flow:      np.ndarray,   # (H, W) uint16
    tile_deposits:  np.ndarray,   # (H, W) uint16
    tile_shore:     np.ndarray,   # (H, W) bool
    tile_biomes:    np.ndarray,   # (H, W) object str
    tile_mc_biomes: np.ndarray,   # (H, W) object str
    tile_origin_x:  int,
    tile_origin_y:  int,
    noise_gens:     dict,
    cfg:            dict,
) -> list[list]:
    """
    Vectorized replacement for process_tile_columns().
    Returns list[list[ColumnResult]] — identical structure to original.
    ~100x faster than the per-column Python loop.
    """
    # ColumnResult, gaea_to_mc_y, SEA_LEVEL are all defined in this same module.
    H, W = tile_height.shape

    # World-space coordinate arrays
    col_idx = np.arange(W, dtype=np.int32)
    row_idx = np.arange(H, dtype=np.int32)
    world_x = (tile_origin_x + col_idx)[np.newaxis, :] * np.ones((H, 1), dtype=np.int32)
    world_z = (tile_origin_y + row_idx)[:, np.newaxis] * np.ones((1, W), dtype=np.int32)

    # ---- 1. Height remap (vectorized LUT) ----
    surface_y = _LUT[tile_height].astype(np.int16)   # (H, W)

    # ---- DIAGNOSTIC guard: tile (32, 2) → col_off=16384, row_off=1024 ----
    _DIAG = (tile_origin_x == 16384 and tile_origin_y == 1024)
    if _DIAG:
        print(f"\n{'='*60}", flush=True)
        print(f"DIAGNOSTIC — tile (32,2)  origin=({tile_origin_x},{tile_origin_y})", flush=True)
        print(f"{'='*60}", flush=True)
        print(f"[Step 1] surface_y (post-LUT, MC Y space):", flush=True)
        print(f"  min={int(surface_y.min())}  max={int(surface_y.max())}  mean={surface_y.mean():.1f}", flush=True)

    # ---- 1a. Ocean depth correction (distance-transform blend) ----
    # Ensures ocean pixels are at least min_ocean_depth blocks below sea level,
    # blending in over transition_px blocks from the shoreline.
    _od_cfg         = cfg.get("ocean_depth", {})
    transition_px   = _od_cfg.get("transition_px", 30)
    min_ocean_depth = _od_cfg.get("min_depth", 15)

    land_mask  = surface_y >= SEA_LEVEL
    dist       = distance_transform_edt(~land_mask).astype(np.float32)
    blend      = np.clip(dist / transition_px, 0.0, 1.0)
    min_sy     = SEA_LEVEL - min_ocean_depth                        # e.g. Y=48
    corrected  = np.minimum(surface_y, min_sy).astype(np.int32)
    surface_y  = np.where(
        ~land_mask,
        np.round(surface_y * (1.0 - blend) + corrected * blend).astype(np.int32),
        surface_y,
    ).astype(np.int16)

    # ---- 2. Normalize float masks ----
    slope_f    = tile_slope.astype(np.float32)    / 65535.0
    erosion_f  = tile_erosion.astype(np.float32)  / 65535.0
    flow_f     = tile_flow.astype(np.float32)     / 65535.0

    if _DIAG:
        # Step 2: slope.tif (normalized [0,1])
        print(f"\n[Step 2 & 5] slope_array from slope.tif (normalized 0-1):", flush=True)
        print(f"  min={slope_f.min():.4f}  max={slope_f.max():.4f}  mean={slope_f.mean():.4f}", flush=True)
        # Step 5: gradient-derived slope from surface_y (MC Y-space)
        _gy, _gx = np.gradient(surface_y.astype(np.float32))
        _grad_deg = np.degrees(np.arctan(np.hypot(_gx, _gy)))
        print(f"[Step 5] gradient(surface_y) slope in degrees:", flush=True)
        print(f"  min={_grad_deg.min():.2f}°  max={_grad_deg.max():.2f}°  mean={_grad_deg.mean():.2f}°", flush=True)
        _diag_thr = cfg.get("slope_surface", {}).get("stone_cliff_threshold_degrees", 55.0)
        print(f"  cliff_deg_thr (stone_cliff_threshold_degrees): {_diag_thr}°", flush=True)
        del _gy, _gx, _grad_deg, _diag_thr

    sc = cfg.get("snow_cap", {})
    bm = cfg.get("block_mixing", {})
    ss = cfg.get("slope_surface", {})
    sh = cfg.get("shoreline", {})
    of = cfg.get("ocean_floor", {})
    sd_cfg = cfg.get("sand_dunes", {})

    snow_line_y    = sc.get("snow_line_y", 320)
    stone_hard     = ss.get("stone_hard_threshold", 0.65)       # kept for snow_layer compat
    grass_hard     = ss.get("grass_hard_threshold", 0.35)
    cliff_deg_thr  = float(ss.get("stone_cliff_threshold_degrees", 55.0))
    erosion_thr    = bm.get("erosion_threshold",  0.65)
    moisture_thr = bm.get("moisture_threshold", 0.60)
    noise_thr    = bm.get("noise_threshold",    0.55)
    noise_scale  = bm.get("noise_scale",        25)
    sand_depth   = of.get("sand_max_depth",     8)
    gravel_depth = of.get("gravel_max_depth",   20)
    beach_width  = sh.get("sand_beach_width_blocks", 4)

    is_ocean = surface_y <= SEA_LEVEL   # (H, W) bool

    # ---- 3. Dune height offset (SAND_DUNE_DESERT only) ----
    dune_mask = tile_biomes == "SAND_DUNE_DESERT"
    dune_add  = np.zeros((H, W), dtype=np.int16)
    if dune_mask.any() and noise_gens:
        gen_a = noise_gens.get("dune_a")
        gen_b = noise_gens.get("dune_b")
        if gen_a and gen_b:
            sa = sd_cfg.get("scale_a", 40)
            sb = sd_cfg.get("scale_b", 18)
            aa = sd_cfg.get("amplitude_a", 1.0)
            ab = sd_cfg.get("amplitude_b", 0.5)
            max_add = sd_cfg.get("max_height_add", 15)
            # Only evaluate noise for actual dune pixels (not the full tile)
            dune_rs, dune_cs = np.where(dune_mask)
            wx_d = world_x[dune_rs, dune_cs].astype(np.float64)
            wz_d = world_z[dune_rs, dune_cs].astype(np.float64)
            na_vals = np.array([gen_a.noise2(float(wx_d[i]/sa), float(wz_d[i]/sa))
                                for i in range(len(dune_rs))], dtype=np.float32)
            nb_vals = np.array([gen_b.noise2(float(wx_d[i]/sb), float(wz_d[i]/sb))
                                for i in range(len(dune_rs))], dtype=np.float32)
            raw_dune_vals = ((na_vals * aa + nb_vals * ab + aa + ab) / (2*(aa+ab))) * max_add
            dune_add[dune_rs, dune_cs] = np.clip(raw_dune_vals, 0, max_add).astype(np.int16)
            surface_y = np.clip(surface_y.astype(np.int32) + dune_add,
                                MC_Y_MIN + 4, MC_Y_MAX - 1).astype(np.int16)
            is_ocean = surface_y <= SEA_LEVEL

    # Cliff slope in degrees from final surface_y (post-LUT, post-dune, MC Y space)
    _gy_c, _gx_c = np.gradient(surface_y.astype(np.float32))
    cliff_deg = np.degrees(np.arctan(np.hypot(_gx_c, _gy_c))).astype(np.float32)
    del _gy_c, _gx_c

    # ---- 4. Block mixing noise (4 layers at different phases) ----
    # Use decoration_density gen if available; fall back to slope_mix.
    _ng = noise_gens.get("decoration_density") or noise_gens.get("slope_mix")

    def _make_noise(ox: int, oz: int, scale: float, octaves: int = 3) -> np.ndarray:
        """
        Pure-numpy fBm value noise.  O(H*W) numpy ops — no Python loops over pixels.
        Tile-consistent: value depends only on world coordinates, not tile position.
        """
        wx = world_x[0, :].astype(np.float32) + ox   # (W,)
        wz = world_z[:, 0].astype(np.float32) + oz   # (H,)
        accum     = np.zeros((H, W), dtype=np.float32)
        amplitude = 1.0
        frequency = 1.0
        total_amp = 0.0

        for oct_i in range(octaves):
            s  = np.float32(scale / frequency)
            x  = wx / s                                       # (W,)
            z  = wz / s                                       # (H,)
            xi = np.floor(x).astype(np.int32)                # (W,) cell col
            zi = np.floor(z).astype(np.int32)                # (H,) cell row
            xf = (x - xi).astype(np.float32)                 # (W,) frac
            zf = (z - zi).astype(np.float32)                 # (H,) frac
            # Smoothstep
            xf = xf * xf * (3.0 - 2.0 * xf)
            zf = zf * zf * (3.0 - 2.0 * zf)

            # Hash two integers → float32 in [-1, 1].
            # Uses uint32 Knuth multiply + avalanche; salt per octave.
            SALT = np.uint32((oct_i * 0x4D35 + 0x1337) & 0xFFFFFFFF)
            def _h(cx: np.ndarray, cz: np.ndarray) -> np.ndarray:
                h = cx.astype(np.uint32) * np.uint32(2654435761)
                h ^= cz.astype(np.uint32) * np.uint32(2246822519)
                h += SALT
                h ^= h >> np.uint32(16)
                h *= np.uint32(0x45D9F3B)
                h ^= h >> np.uint32(16)
                return h.astype(np.float32) * np.float32(4.656612873e-10) - 1.0

            # Build 2-D grids of cell indices
            xi_g, zi_g = np.meshgrid(xi, zi, indexing="xy")  # (H, W)
            xf_g, zf_g = np.meshgrid(xf, zf, indexing="xy")  # (H, W)

            v00 = _h(xi_g,     zi_g    )
            v10 = _h(xi_g + 1, zi_g    )
            v01 = _h(xi_g,     zi_g + 1)
            v11 = _h(xi_g + 1, zi_g + 1)

            layer = (v00 * (1 - xf_g) * (1 - zf_g)
                   + v10 * xf_g       * (1 - zf_g)
                   + v01 * (1 - xf_g) * zf_g
                   + v11 * xf_g       * zf_g)

            accum     += layer * np.float32(amplitude)
            total_amp += amplitude
            amplitude *= 0.5
            frequency *= 2.0

        return (accum / np.float32(total_amp) + 1.0) / 2.0

    den_scale = cfg.get("decoration_density_noise", {}).get("scale", noise_scale)
    mix_noise  = _make_noise(0,      0,      noise_scale)   # primary
    mix_noise2 = _make_noise(31337,  71193,  den_scale)     # secondary
    mix_noise3 = _make_noise(99991,  13337,  den_scale)     # tertiary
    mix_noise4 = _make_noise(777771, 444443, den_scale)     # sparse

    # Additional thresholds for the full 8-condition palette system
    erosion_thr2  = bm.get("erosion2_threshold",  0.010)
    moisture_thr2 = bm.get("moisture2_threshold", 0.020)
    noise_thr2    = bm.get("noise2_threshold",    0.75)
    noise_thr3    = bm.get("noise3_threshold",    0.82)
    noise_thr4    = bm.get("noise4_threshold",    0.88)

    if _DIAG:
        print(f"\n[Step 1] noise raw values (all 4 layers, land+ocean):", flush=True)
        print(f"  mix_noise  (primary,   scale={noise_scale}): min={mix_noise.min():.4f}  max={mix_noise.max():.4f}  mean={mix_noise.mean():.4f}", flush=True)
        print(f"  mix_noise2 (secondary, scale={den_scale}): min={mix_noise2.min():.4f}  max={mix_noise2.max():.4f}  mean={mix_noise2.mean():.4f}", flush=True)
        print(f"  mix_noise3 (tertiary,  scale={den_scale}): min={mix_noise3.min():.4f}  max={mix_noise3.max():.4f}  mean={mix_noise3.mean():.4f}", flush=True)
        print(f"  mix_noise4 (sparse,    scale={den_scale}): min={mix_noise4.min():.4f}  max={mix_noise4.max():.4f}  mean={mix_noise4.mean():.4f}", flush=True)

    # Pre-build boolean condition arrays (H×W)
    _conds: dict[str, np.ndarray] = {
        "noise":     mix_noise  > noise_thr,
        "noise2":    mix_noise2 > noise_thr2,
        "noise3":    mix_noise3 > noise_thr3,
        "noise4":    mix_noise4 > noise_thr4,
        "moisture":  flow_f     > moisture_thr,
        "moisture2": flow_f     > moisture_thr2,
        "erosion":   erosion_f  > erosion_thr,
        "erosion2":  erosion_f  > erosion_thr2,
        "altitude":  surface_y  >= snow_line_y,
    }

    # Step 3: disable noise conditions when flag is set (diagnostic only)
    if _DEBUG_DISABLE_NOISE:
        _zero = np.zeros((H, W), dtype=bool)
        _conds["noise"]  = _zero
        _conds["noise2"] = _zero
        _conds["noise3"] = _zero
        _conds["noise4"] = _zero

    if _DIAG:
        print(f"\n[Step 1] Condition coverage (all pixels, thr from config):", flush=True)
        print(f"  noise  (>{noise_thr:.3f}):    {_conds['noise'].mean()*100:.1f}%", flush=True)
        print(f"  noise2 (>{noise_thr2:.3f}):   {_conds['noise2'].mean()*100:.1f}%", flush=True)
        print(f"  noise3 (>{noise_thr3:.3f}):   {_conds['noise3'].mean()*100:.1f}%", flush=True)
        print(f"  noise4 (>{noise_thr4:.3f}):   {_conds['noise4'].mean()*100:.1f}%", flush=True)
        print(f"  moisture  (>{moisture_thr:.3f}): {_conds['moisture'].mean()*100:.1f}%", flush=True)
        print(f"  moisture2 (>{moisture_thr2:.3f}): {_conds['moisture2'].mean()*100:.1f}%", flush=True)
        print(f"  erosion   (>{erosion_thr:.3f}): {_conds['erosion'].mean()*100:.1f}%", flush=True)
        print(f"  erosion2  (>{erosion_thr2:.3f}): {_conds['erosion2'].mean()*100:.1f}%", flush=True)
        print(f"  altitude  (>=Y{snow_line_y}):    {_conds['altitude'].mean()*100:.1f}%", flush=True)
        print(f"  cliff_deg(>={cliff_deg_thr:.0f}°): {(cliff_deg>=cliff_deg_thr).mean()*100:.1f}%  ← gradient-degree cliff override", flush=True)
        print(f"  _DEBUG_DISABLE_NOISE = {_DEBUG_DISABLE_NOISE}", flush=True)

    # ---- 5. Surface + subsurface block arrays (surface_decorator palettes) ----
    # Import the approved rich per-biome palettes from surface_decorator.
    from core.surface_decorator import BIOME_BLOCK_PALETTES as _SD_PALETTES

    surface_blk = np.full((H, W), "grass_block", dtype=object)
    sub_blk     = np.full((H, W), "dirt",        dtype=object)

    # Priority order: base first (lowest), each tag overwrites previous.
    # "altitude" is excluded — it's applied as Step 6b AFTER the slope override,
    # so elevation/snow has final say over cliff stone.
    _PRIORITY = [
        "noise4", "noise3", "noise2", "noise",
        "moisture2", "moisture",
        "erosion2", "erosion",
    ]

    for biome_name in np.unique(tile_biomes):
        bname = str(biome_name)
        bmask = tile_biomes == biome_name
        if not bmask.any():
            continue

        palette = _SD_PALETTES.get(bname)
        if palette is None:
            base_surf, base_sub = _BIOME_BASE_BLOCKS.get(bname, _DEFAULT_SURFACE)
            surface_blk[bmask] = base_surf
            sub_blk[bmask]     = base_sub
            continue

        # Index palette by condition tag (last entry per tag wins if duplicated)
        by_tag: dict[str, tuple[str, str]] = {}
        for surf, sub, tag in palette:
            by_tag[tag] = (surf, sub)

        # Apply base first
        base_s, base_b = by_tag.get("base", ("grass_block", "dirt"))
        surface_blk[bmask] = base_s
        sub_blk[bmask]     = base_b

        # Apply remaining conditions in priority order (each overwrites previous)
        for tag in _PRIORITY:
            entry = by_tag.get(tag)
            if entry is None:
                continue
            apply = bmask & _conds.get(tag, np.zeros((H, W), dtype=bool))
            if apply.any():
                surface_blk[apply] = entry[0]
                sub_blk[apply]     = entry[1]

    # ---- 6. Slope zones — 3-zone system (Norterre-inspired) ----
    # Zone 1 (< full_grass_max_deg): full biome surface palette (unchanged)
    # Zone 2 (transition): mixed biome/stone scatter — the "rocky slope" look
    # Zone 3 (> full_cliff_min_deg): full cliff stone with banding
    land = ~is_ocean
    sz = cfg.get("slope_zones", {})
    grass_max_deg  = float(sz.get("full_grass_max_deg",    25))
    trans_max_deg  = float(sz.get("transition_max_deg",    50))
    trans_stone_f  = float(sz.get("transition_stone_frac", 0.5))

    desert_mask = np.isin(tile_biomes, list(DESERT_BIOMES))
    stone_surf  = np.where(desert_mask, "sandstone", "stone")

    # Zone 2: transition slope — scatter stone using noise
    transition_zone = land & (cliff_deg >= grass_max_deg) & (cliff_deg < trans_max_deg)
    if transition_zone.any():
        # Use mix_noise to decide which pixels become stone in the transition
        stone_scatter = transition_zone & (mix_noise2 < trans_stone_f)
        surface_blk[stone_scatter] = stone_surf[stone_scatter]
        sub_blk[stone_scatter]     = "stone"
        # Gravel scatter at higher slope within transition
        gravel_scatter = transition_zone & ~stone_scatter & (cliff_deg >= (grass_max_deg + trans_max_deg) / 2)
        gravel_mask = gravel_scatter & (mix_noise3 < 0.4)
        surface_blk[gravel_mask] = "gravel"
        sub_blk[gravel_mask]     = "stone"

    # Zone 3: full cliff — hard stone override
    hard_stone = land & (cliff_deg >= trans_max_deg)
    if hard_stone.any():
        surface_blk[hard_stone] = stone_surf[hard_stone]
        sub_blk[hard_stone]     = stone_surf[hard_stone]

    # ---- 6a. Talus / scree at cliff bases ----
    # Detect pixels adjacent to cliffs but low-slope themselves.
    # Place gravel/cobblestone scatter to simulate accumulated rock debris.
    talus_cfg = cfg.get("talus", {})
    if talus_cfg.get("enabled", True) and hard_stone.any():
        talus_dilate = int(talus_cfg.get("dilate_px", 8))
        talus_gravel_f = float(talus_cfg.get("gravel_frac", 0.5))
        # Dilate cliff mask, intersect with non-cliff low-slope land
        cliff_dilated = binary_dilation(hard_stone, iterations=talus_dilate)
        talus_zone = land & cliff_dilated & ~hard_stone & (cliff_deg < grass_max_deg)
        if talus_zone.any():
            # Distance-based falloff: closer to cliff = more debris
            talus_scatter = talus_zone & (mix_noise3 < 0.6)
            gravel_talus = talus_scatter & (mix_noise4 < talus_gravel_f)
            cobble_talus = talus_scatter & ~gravel_talus
            surface_blk[gravel_talus] = "gravel"
            sub_blk[gravel_talus]     = "stone"
            surface_blk[cobble_talus] = "cobblestone"
            sub_blk[cobble_talus]     = "stone"

    # ---- 6b. Altitude override (runs AFTER slope — highest priority on land) ----
    # High-elevation pixels get altitude-tagged palette block even on steep faces.
    if _conds["altitude"].any():
        for _b_alt in np.unique(tile_biomes):
            _bname_alt = str(_b_alt)
            _bmask_alt = (tile_biomes == _b_alt) & land & _conds["altitude"]
            if not _bmask_alt.any():
                continue
            _pal_alt = _SD_PALETTES.get(_bname_alt)
            if _pal_alt is None:
                continue
            _by_tag_alt = {t: (s, sb) for s, sb, t in _pal_alt}
            _entry_alt = _by_tag_alt.get("altitude")
            if _entry_alt is None:
                continue
            surface_blk[_bmask_alt] = _entry_alt[0]
            sub_blk[_bmask_alt]     = _entry_alt[1]

    # ---- 6c. High alpine bare-rock exposure ----
    # Above exposure_y (±noise jitter), surface and subsurface become bare rock.
    # Gives high peaks a geologically raw, windswept look.
    _geo = cfg.get("geo_surface", {})
    _expo_y   = int(_geo.get("alpine_exposure_y", 340))
    _expo_amp = int(_geo.get("alpine_exposure_noise_amp", 8))
    if _expo_amp > 0 and mix_noise is not None:
        # Vary the exposure threshold slightly per column using mix_noise
        expo_threshold = (_expo_y + (mix_noise * 2 - 1.0) * _expo_amp).astype(np.float32)
    else:
        expo_threshold = np.full((H, W), _expo_y, dtype=np.float32)
    high_alpine = land & (surface_y.astype(np.float32) >= expo_threshold)
    if high_alpine.any():
        # Use noise to scatter between stone, cobblestone, and gravel
        # Use mix_noise2/mix_noise3 (different phases from step 4)
        _stone_frac  = (mix_noise2 < 0.50)  # 50 % stone
        _cobble_frac = (mix_noise2 >= 0.50) & (mix_noise2 < 0.75)  # 25 % cobblestone
        # remaining 25 % → gravel
        surface_blk = np.where(high_alpine & _stone_frac,  "stone",       surface_blk)
        surface_blk = np.where(high_alpine & _cobble_frac, "cobblestone", surface_blk)
        surface_blk = np.where(high_alpine & ~_stone_frac & ~_cobble_frac, "gravel", surface_blk)
        sub_blk     = np.where(high_alpine, "stone", sub_blk)

    # ---- 6d. Frost-shattered ridgeline scatter ----
    # Just below the exposure threshold (frost_ridge_y to exposure_y), steep ridges
    # get scattered cobblestone to simulate frost-fractured stone.
    _frost_y   = int(_geo.get("frost_ridge_y", 300))
    _frost_deg = float(_geo.get("frost_ridge_deg", 35.0))
    _frost_chance = float(_geo.get("frost_cobble_chance", 0.45))
    frost_ridge = (land
                   & (surface_y.astype(np.int32) >= _frost_y)
                   & (surface_y.astype(np.float32) < expo_threshold)
                   & (cliff_deg >= _frost_deg))
    if frost_ridge.any():
        # Scatter cobblestone and stone — use mix_noise3 for the spatial pattern
        _cobble_here = frost_ridge & (mix_noise3 < _frost_chance)
        _stone_here  = frost_ridge & (mix_noise3 >= _frost_chance) & (mix_noise3 < (_frost_chance + 0.25))
        surface_blk = np.where(_cobble_here, "cobblestone", surface_blk)
        surface_blk = np.where(_stone_here,  "stone",       surface_blk)

    # ---- 7. Shoreline gradient (Norterre-inspired multi-band coastal transition) ----
    sg = cfg.get("shoreline_gradient", {})
    sg_sand    = int(sg.get("sand_height",       3))
    sg_gravel  = int(sg.get("gravel_height",     6))
    sg_trans   = int(sg.get("transition_height", 10))
    dist_above = (surface_y.astype(np.int32) - SEA_LEVEL).astype(np.int16)
    # Band 1: sand (0 to sg_sand blocks above sea)
    beach_sand = land & tile_shore & (dist_above >= 0) & (dist_above <= sg_sand)
    surface_blk[beach_sand] = "sand"
    sub_blk[beach_sand]     = "sandstone"
    # Band 2: gravel/clay mix (sg_sand to sg_gravel)
    beach_gravel = land & tile_shore & (dist_above > sg_sand) & (dist_above <= sg_gravel)
    if beach_gravel.any():
        gravel_here = beach_gravel & (mix_noise2 < 0.6)
        clay_here   = beach_gravel & ~gravel_here
        surface_blk[gravel_here] = "gravel"
        sub_blk[gravel_here]     = "stone"
        surface_blk[clay_here]   = "clay"
        sub_blk[clay_here]       = "dirt"
    # Band 3: coarse_dirt transition (sg_gravel to sg_trans)
    beach_trans = land & tile_shore & (dist_above > sg_gravel) & (dist_above <= sg_trans)
    if beach_trans.any():
        trans_here = beach_trans & (mix_noise3 < 0.5)
        surface_blk[trans_here] = "coarse_dirt"
        sub_blk[trans_here]     = "gravel"

    # ---- 7b. Flow-path sand deposition (desert biomes only) ----
    # In arid biomes, moderate flow values trace drainage paths where sand accumulates.
    # Replaces whatever surface block was set with sand along flow corridors.
    if desert_mask.any():
        flow_sand = desert_mask & land & (flow_f >= 0.15) & (flow_f < 0.50) & (cliff_deg < grass_max_deg)
        if flow_sand.any():
            surface_blk[flow_sand] = "sand"
            sub_blk[flow_sand]     = "sandstone"

    # ---- 8. Snow cap ----
    snow_biomes_nosnow = np.isin(tile_biomes,
        ["SNOWY_BOREAL_TAIGA","ARCTIC_TUNDRA","FROZEN_FLATS"])
    snow_layer = (land
                  & ~snow_biomes_nosnow
                  & (surface_y >= snow_line_y)
                  & (cliff_deg < trans_max_deg))

    # ---- 9. Ocean / river floor (noisy material mix) ----
    depth_below = np.where(is_ocean,
                           SEA_LEVEL - surface_y.astype(np.int32), 0).astype(np.int16)
    # Noise-modulated floor: instead of uniform sand/gravel/stone by depth,
    # use mix_noise to scatter mud, clay, dirt, sand, gravel within each zone.
    # Shallow (0-sand_depth): mostly sand, scatter of clay/mud
    # Mid (sand_depth-gravel_depth): gravel dominant, scatter of sand/clay/mud
    # Deep (>gravel_depth): stone dominant, scatter of gravel/clay
    shallow = is_ocean & (depth_below <= sand_depth)
    mid_depth = is_ocean & (depth_below > sand_depth) & (depth_below <= gravel_depth)
    deep = is_ocean & (depth_below > gravel_depth)

    # Shallow floor: 55% sand, 15% clay, 15% mud, 10% gravel, 5% dirt
    if shallow.any():
        sh_sand  = shallow & (mix_noise < 0.55)
        sh_clay  = shallow & (mix_noise >= 0.55) & (mix_noise < 0.70)
        sh_mud   = shallow & (mix_noise >= 0.70) & (mix_noise < 0.85)
        sh_grav  = shallow & (mix_noise >= 0.85) & (mix_noise < 0.95)
        sh_dirt  = shallow & (mix_noise >= 0.95)
        surface_blk[sh_sand] = "sand"
        surface_blk[sh_clay] = "clay"
        surface_blk[sh_mud]  = "mud"
        surface_blk[sh_grav] = "gravel"
        surface_blk[sh_dirt] = "dirt"
        sub_blk[shallow] = "sandstone"

    # Mid-depth floor: 40% gravel, 25% sand, 20% clay, 10% mud, 5% stone
    if mid_depth.any():
        md_grav  = mid_depth & (mix_noise2 < 0.40)
        md_sand  = mid_depth & (mix_noise2 >= 0.40) & (mix_noise2 < 0.65)
        md_clay  = mid_depth & (mix_noise2 >= 0.65) & (mix_noise2 < 0.85)
        md_mud   = mid_depth & (mix_noise2 >= 0.85) & (mix_noise2 < 0.95)
        md_stone = mid_depth & (mix_noise2 >= 0.95)
        surface_blk[md_grav]  = "gravel"
        surface_blk[md_sand]  = "sand"
        surface_blk[md_clay]  = "clay"
        surface_blk[md_mud]   = "mud"
        surface_blk[md_stone] = "stone"
        sub_blk[mid_depth] = "stone"

    # Deep floor: 60% stone, 20% gravel, 10% clay, 10% mud
    if deep.any():
        dp_stone = deep & (mix_noise3 < 0.60)
        dp_grav  = deep & (mix_noise3 >= 0.60) & (mix_noise3 < 0.80)
        dp_clay  = deep & (mix_noise3 >= 0.80) & (mix_noise3 < 0.90)
        dp_mud   = deep & (mix_noise3 >= 0.90)
        surface_blk[dp_stone] = "stone"
        surface_blk[dp_grav]  = "gravel"
        surface_blk[dp_clay]  = "clay"
        surface_blk[dp_mud]   = "mud"
        sub_blk[deep] = "stone"

    # Near-shore override: sand + sandstone (beach transition into water)
    near_shore_ocean = is_ocean & tile_shore & (depth_below <= 2)
    surface_blk[near_shore_ocean] = "sand"
    sub_blk[near_shore_ocean]     = "sandstone"

    if _DIAG:
        # Step 4: block frequency (land only — tells us what is dominating)
        _land = ~is_ocean
        _land_blk = surface_blk[_land]
        _uniq, _cnts = np.unique(_land_blk, return_counts=True)
        _order = np.argsort(-_cnts)
        _total_land = int(_land.sum())
        print(f"\n[Step 4] Surface block frequency — land pixels only ({_total_land} total):", flush=True)
        for _i in _order[:15]:
            _pct = _cnts[_i] / _total_land * 100
            print(f"  {str(_uniq[_i]):30s}  {_cnts[_i]:7d}  ({_pct:5.1f}%)", flush=True)
        # Which condition drove majority?
        _cliff_stone = int((land & (cliff_deg >= cliff_deg_thr)).sum())
        _noise_any   = int((_conds["noise"] | _conds["noise2"] | _conds["noise3"] | _conds["noise4"]).sum())
        print(f"\n  Pixels overridden by cliff (>={cliff_deg_thr:.0f}°): {_cliff_stone} ({_cliff_stone/_total_land*100:.1f}% of land)", flush=True)
        print(f"  Pixels with any noise condition true: {_noise_any} ({_noise_any/(_land.size)*100:.1f}% of all)", flush=True)
        print(f"\n[Step 4] Masking order (lowest→highest priority, FIXED):", flush=True)
        print(f"  1. base biome block", flush=True)
        print(f"  2. noise4/noise3/noise2/noise", flush=True)
        print(f"  3. moisture2/moisture", flush=True)
        print(f"  4. erosion2/erosion", flush=True)
        print(f"  5. slope cliff override (>={cliff_deg_thr:.0f}° gradient degrees)", flush=True)
        print(f"  6. altitude (snow_line_y={snow_line_y}) ← HIGHEST, runs after slope (Step 6b)", flush=True)
        print(f"  7. shoreline/beach", flush=True)
        print(f"  8. ocean floor", flush=True)
        print(f"{'='*60}\n", flush=True)

    # ---- 10. Build ColumnResult grid ----
    # We return list[list[ColumnResult]] for API compatibility,
    # but blocks dict only contains the sparse non-default entries.
    # ColumnResult is defined in this same module — no import needed.
    results = []
    sea = int(SEA_LEVEL)
    for r in range(H):
        row_results = []
        sy_row   = surface_y[r]       # (W,) int16 view — avoids 2-D index per col
        surf_row = surface_blk[r]
        sub_row  = sub_blk[r]
        snow_row = snow_layer[r]
        mc_row   = tile_mc_biomes[r]
        dune_row = dune_add[r]
        for c in range(W):
            sy   = int(sy_row[c])
            surf = str(surf_row[c])
            sub  = str(sub_row[c])
            snow = bool(snow_row[c])
            mc_b = str(mc_row[c])

            # Sparse blocks dict — only surface/sub, water fill, bedrock
            blks: dict[int, str] = {BEDROCK_Y: "bedrock", sy: surf}
            if sy - 2 > BEDROCK_Y: blks[sy - 2] = sub
            if sy - 1 > BEDROCK_Y: blks[sy - 1] = sub

            if sy < sea:
                blks.update({y: "water" for y in range(sy + 1, sea + 1)})
            elif sy == sea:
                blks[sea] = surf   # shore pixel — already set, no water fill needed

            dd = int(dune_row[c])
            if dd > 0:
                orig = sy - dd
                blks.update({y: "sand" for y in range(orig + 1, sy)})

            row_results.append(ColumnResult(
                surface_y  = sy,
                blocks     = blks,
                snow_layer = snow,
                biome_id   = mc_b,
            ))

        results.append(row_results)
    return results


# ---------------------------------------------------------------------------
# PIPELINE-FACING WRAPPER  (called by run_pipeline.py Step 6)
# ---------------------------------------------------------------------------

def generate_columns(
    height_tile:  np.ndarray,   # (H, W) uint16 — raw Gaea height
    slope_tile:   np.ndarray,   # (H, W) uint16 — not used for surface_y but kept for API compat
    biome_grid:   np.ndarray,   # (H, W) str    — for dune height offsets
    shore_tile:   np.ndarray,   # (H, W) bool   — not used for surface_y but kept for API compat
    noise_fields: dict,         # noise generators (may be None/empty)
    cfg:          dict,
    tile_x:       int,          # tile index X (multiply by 512 for world origin)
    tile_y:       int,          # tile index Y (multiply by 512 for world origin)
) -> np.ndarray:
    """
    Compute the surface_y (H, W) int16 array for a tile.
    Applies: height LUT → ocean-depth correction → dune height offset.
    Returns shape (H, W) int16 with MC Y values.
    """
    from scipy.ndimage import distance_transform_edt

    H, W = height_tile.shape
    TILE_SZ = 512

    # 1. Height LUT (mask may arrive as float — cast to uint16 first)
    surface_y = _LUT[height_tile.astype(np.uint16)].astype(np.int16)

    # 2. Ocean depth correction
    _od_cfg         = cfg.get("ocean_depth", {})
    transition_px   = _od_cfg.get("transition_px", 30)
    min_ocean_depth = _od_cfg.get("min_depth", 15)

    land_mask = surface_y >= SEA_LEVEL
    dist      = distance_transform_edt(~land_mask).astype(np.float32)
    blend     = np.clip(dist / transition_px, 0.0, 1.0)
    min_sy    = SEA_LEVEL - min_ocean_depth
    corrected = np.minimum(surface_y, min_sy).astype(np.int32)
    surface_y = np.where(
        ~land_mask,
        np.round(surface_y * (1.0 - blend) + corrected * blend).astype(np.int32),
        surface_y,
    ).astype(np.int16)

    # 3. Dune height offset (SAND_DUNE_DESERT only)
    dune_mask = biome_grid == "SAND_DUNE_DESERT"
    if dune_mask.any() and noise_fields:
        sd_cfg  = cfg.get("sand_dunes", {})
        gen_a   = noise_fields.get("dune_a")
        gen_b   = noise_fields.get("dune_b")
        if gen_a and gen_b:
            sa      = sd_cfg.get("scale_a", 40)
            sb      = sd_cfg.get("scale_b", 18)
            aa      = sd_cfg.get("amplitude_a", 1.0)
            ab      = sd_cfg.get("amplitude_b", 0.5)
            max_add = sd_cfg.get("max_height_add", 15)

            tile_origin_x = tile_x * TILE_SZ
            tile_origin_y = tile_y * TILE_SZ
            col_idx = np.arange(W, dtype=np.int32)
            row_idx = np.arange(H, dtype=np.int32)
            world_x = (tile_origin_x + col_idx)[np.newaxis, :] * np.ones((H, 1), dtype=np.int32)
            world_z = (tile_origin_y + row_idx)[:, np.newaxis] * np.ones((1, W), dtype=np.int32)

            dune_rs, dune_cs = np.where(dune_mask)
            wx_d = world_x[dune_rs, dune_cs].astype(np.float64)
            wz_d = world_z[dune_rs, dune_cs].astype(np.float64)
            na_vals = np.array([gen_a.noise2(float(wx_d[i]/sa), float(wz_d[i]/sa))
                                for i in range(len(dune_rs))], dtype=np.float32)
            nb_vals = np.array([gen_b.noise2(float(wx_d[i]/sb), float(wz_d[i]/sb))
                                for i in range(len(dune_rs))], dtype=np.float32)
            raw_dune = ((na_vals * aa + nb_vals * ab + aa + ab) / (2*(aa+ab))) * max_add
            dune_add = np.zeros((H, W), dtype=np.int16)
            dune_add[dune_rs, dune_cs] = np.clip(raw_dune, 0, max_add).astype(np.int16)
            surface_y = np.clip(
                surface_y.astype(np.int32) + dune_add,
                MC_Y_MIN + 4, MC_Y_MAX - 1,
            ).astype(np.int16)

    return surface_y
