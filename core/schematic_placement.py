"""
schematic_placement.py — Step 8: Schematic Placement
Vandir World Generation Pipeline — /core/schematic_placement.py

Responsibilities:
  - Load schematic_index.json (produced by merge_anchor_index.py)
  - For each land pixel in the tile, select a schematic via weighted random
    choice from the biome's palette
  - Compute world-space placement Y using anchor system + Y-variation
  - Enforce canopy overlap exclusion zones (radius-based)
  - Exclude river bank pixels (river_meta > 0)
  - Return a list of PlacementRecord for the chunk writer to stamp

Inputs:
  - surface_y:       (H, W) int16 — terrain surface Y per pixel
  - biome_grid:      (H, W) object — resolved biome name per pixel
  - river_meta:      (H, W) uint8 — bank pixels from river_carver
  - moisture_tile:   (H, W) float32 — moisture (flow) proxy mask
  - noise_fields:    dict of OpenSimplex generators
  - cfg:             dict from thresholds.json
  - index:           dict — loaded schematic_index.json (call load_index once)
  - tile_x, tile_y:  int — for seeded RNG

Output:
  - list[PlacementRecord] — each is a dataclass with all info chunk_writer needs

Arch rules:
  - No GUI imports
  - No full raster loads
  - Seeded RNG per tile: seed = tile_x * 73856093 ^ tile_y * 19349663 ^ GLOBAL_SEED
  - All index lookups O(1) — index pre-grouped by biome at load time
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

GLOBAL_SEED: int = 42000  # matches pipeline global seed

# Size code → (weight, extra_inset_distribution)
# extra_inset: additional blocks to sink beyond mandatory inset_depth
# Weights: +0, +1, +2
SIZE_VARIATION: dict[str, tuple[int, list[float]]] = {
    "sm": (3, [0.80, 0.18, 0.02]),
    "md": (2, [0.60, 0.30, 0.10]),
    "lg": (1, [0.40, 0.40, 0.20]),
}

# Biomes excluded from bush fallback (already injected at index build time,
# but double-guard here)
NO_BUSH_BIOMES: frozenset[str] = frozenset({
    # S70: removed FROZEN_FLATS — user wants sparse bush+dead-grass on the
    # snow surface (still sparse but not zero).
    "ARCTIC_TUNDRA",
})

# Sparse bush biomes — halve base density for bush entries
SPARSE_BUSH_BIOMES: frozenset[str] = frozenset({
    "MANGROVE_COAST",
})

# S63: aesthetic-conflict biome pairs — the S59 ecotone seam dither will NOT
# swap across these pairs (either direction).  Purpose: prevent jarring
# species crossovers like temperate-rainforest conifers bleeding into tropical
# jungle (LUSH_RAINFOREST_COAST).  Each entry is an unordered pair.
ECOTONE_DENY_PAIRS: frozenset[frozenset[str]] = frozenset({
    # conifer → tropical jungle
    frozenset({"TEMPERATE_RAINFOREST", "LUSH_RAINFOREST_COAST"}),
    frozenset({"TEMPERATE_RAINFOREST", "TIDAL_JUNGLE_FRINGE"}),
    frozenset({"BOREAL_TAIGA", "LUSH_RAINFOREST_COAST"}),
    frozenset({"SNOWY_BOREAL_TAIGA", "LUSH_RAINFOREST_COAST"}),
    # desert/arid → wet tropical/coastal
    frozenset({"SAND_DUNE_DESERT", "LUSH_RAINFOREST_COAST"}),
    frozenset({"SAND_DUNE_DESERT", "MANGROVE_COAST"}),
    frozenset({"SAND_DUNE_DESERT", "TIDAL_JUNGLE_FRINGE"}),
    # temperate-rainforest → coastal tropical RAINFOREST_COAST (ambiguous boundary)
    frozenset({"TEMPERATE_RAINFOREST", "RAINFOREST_COAST"}),
})

# S89: snowy/conifer biomes that must NEVER receive BROADLEAF trees via the
# ecotone seam swap (birch from a BIRCH_FOREST/MIXED_FOREST neighbour was landing
# in the snow). When a cell of one of these swaps to a neighbour, only conifer
# species survive (matched by substring). Conifer-conifer mixing is preserved.
_CONIFER_SNOWY_BIOMES: frozenset[str] = frozenset({
    "SNOWY_BOREAL_TAIGA", "BOREAL_TAIGA", "BOREAL_ALPINE",
    "ARCTIC_TUNDRA", "FROZEN_FLATS",
})
_CONIFER_SPECIES_SUBSTR: tuple[str, ...] = (
    "spruce", "fir", "pine", "tamarack", "larch", "scotsp", "cedar", "hemlock",
)

# Canopy overlap exclusion radii (pixels) per size code
CANOPY_RADIUS: dict[str, int] = {
    "sm": 3,
    "md": 5,
    "lg": 8,
}

# S96 tree-canopy SEAM fix (STEP B): world-coordinate splitmix64 hash.
# Scalar mirror of the vectorized hash in run_pipeline.py:611-620.  Two
# X-adjacent tiles compute IDENTICAL values for a shared seam cell (same
# world_x/world_z + salt -> same u01), so the deterministic rim pass below
# reconstructs the SAME seam trees from both sides; each tile renders its
# in-bounds half (STEP A clip in chunk_writer), closing the canopy trench.
_WC_MASK64 = 0xFFFFFFFFFFFFFFFF


def _wc_u01s(wx: int, wz: int, salt: int) -> float:
    """Deterministic uniform [0,1) keyed on world (x,z) + a per-purpose salt."""
    h = (int(wx) * 0x9E3779B97F4A7C15
         + int(wz) * 0xBF58476D1CE4E5B9
         + int(salt) * 0x165667B19E3779F9) & _WC_MASK64
    h = ((h ^ (h >> 30)) * 0xBF58476D1CE4E5B9) & _WC_MASK64
    h = ((h ^ (h >> 27)) * 0x94D049BB133111EB) & _WC_MASK64
    h = h ^ (h >> 31)
    return (h & _WC_MASK64) / 2.0 ** 64

# S89 Option A: per-biome canopy-radius control. The exclusion radius is the
# TRUE density ceiling (past BASE_DENSITY ~0.5 the forest is packing-limited,
# not probability-limited). radius_mult < 1 packs trees tighter (denser forest);
# clamped to a per-size LOG-SAFE FLOOR so trunks never share a column (leaf
# canopy clipping is fine, trunk clipping is not).
_LOG_SAFE_FLOOR: dict[str, int] = {"sm": 2, "md": 3, "lg": 4}


_KR_HEIGHT_CACHE: dict[str, "int | None"] = {}


def _krummholz_tree_height(path: str) -> "int | None":
    """Tree height in blocks from the LOWEST log to the highest solid block
    (i.e. the real silhouette height). Cached per path. The schematic SIZE codes
    (sm/md/lg) are unreliable -- e.g. dpine_tree_ppine_d_md is 42 tall -- so the
    krummholz gate measures the actual geometry instead. Returns None if the
    schematic can't be loaded (caller then keeps the entry rather than dropping)."""
    if path in _KR_HEIGHT_CACHE:
        return _KR_HEIGHT_CACHE[path]
    h = None
    try:
        from core import schematic_loader as _sl
        sd = _sl.load_schem(Path(path))
        b = sd.blocks  # (Y, Z, X) object str
        low_log = -1
        top = -1
        for yi in range(b.shape[0]):
            plane = b[yi].ravel()
            has_log = False
            has_solid = False
            for c in plane:
                if isinstance(c, str) and "air" not in c:
                    has_solid = True
                    if "_log" in c:
                        has_log = True
                        break
            if has_solid:
                top = yi
            if has_log and low_log < 0:
                low_log = yi
        if low_log >= 0 and top >= low_log:
            h = top - low_log + 1
    except Exception:
        h = None
    _KR_HEIGHT_CACHE[path] = h
    return h


def _biome_canopy_radius(size: str, biome: str, cfg: dict) -> int:
    base = CANOPY_RADIUS.get(size, 4)
    tcfg = cfg.get("tree_spacing", {}) if isinstance(cfg, dict) else {}
    mult = float(tcfg.get("radius_mult_by_biome", {}).get(
        biome, tcfg.get("default_radius_mult", 1.0)))
    floor = int(tcfg.get("log_safe_floor_by_size", {}).get(
        size, _LOG_SAFE_FLOOR.get(size, 2)))
    return max(floor, int(round(base * mult)))

# Base placement density per biome (probability a candidate pixel is attempted)
# Tuned conservatively — final density also multiplied by decoration noise
BASE_DENSITY: dict[str, float] = {
    "COASTAL_HEATH":           0.25,
    "TEMPERATE_RAINFOREST":    0.26,
    "BOREAL_TAIGA":            1.00,   # S88w5 was 0.95 — user: 2x for coniferous forests.
    "SNOWY_BOREAL_TAIGA":      1.00,   # S88w5 was 0.70 — user: 2x for coniferous forests.
    "BOREAL_ALPINE":           1.00,   # S88w5 was 0.55 — user: 2x for coniferous forests.
    # NOTE: gate is `rng.random() >= final_d`, so density saturates at 1.0
    # (100% per-pixel attempt rate).  2x comes from this saturation + the
    # S88w5 slope-bound relaxation (35°→45° start, 50°→65° full) letting
    # more attempts pass the slope reject.
    "ARCTIC_TUNDRA":           0.15,   # S89 walk #8: user raised AT treeline->700; 0.04 left the high
                                       # tundra/plateau ecotone bare. 0.15 = sparse scatter (krummholz
                                       # + density-fade keep it sparse up high). TUNABLE from render.
    "FROZEN_FLATS":            0.04,
    "TEMPERATE_DECIDUOUS":     0.22,
    "RAINFOREST_COAST":        0.80,   # S89 walk: user — up much closer to BA (was 0.32)
    "RIPARIAN_WOODLAND":       0.18,
    "DRY_OAK_SAVANNA":         0.25,   # S89 walk: user — up a bit, too sparse (was 0.15)
    "KARST_BARRENS":           0.70,   # S89: ~2x per user (ref "Mountain rock exposure in valley")
    "BIRCH_FOREST":            1.00,   # S89 walk2: user — way denser; real lever was canopy radius (0.55), BASE saturates
    "EASTERN_TEMPERATE_COAST": 0.06,
    "MIXED_FOREST":            0.32,
    "CONTINENTAL_STEPPE":      0.0005,
    "DRY_PINE_BARRENS":        0.40,   # S87w3 was 0.21 — user: much more close to TRF
    "SCRUBBY_HEATHLAND":       0.06,
    "LUSH_RAINFOREST_COAST":   0.36,
    "SAND_DUNE_DESERT":        0.020,
    "DESERT_STEPPE_TRANSITION":0.015,
    "SEMI_ARID_SHRUBLAND":     0.0008,
    "DRY_WOODLAND_MAQUIS":     0.015,
    "TIDAL_JUNGLE_FRINGE":     0.15,
    "MANGROVE_COAST":          0.14,
    "FRESHWATER_FEN":          0.12,
}


# S86 Item 3C: per-biome bush density multiplier.  Tree density above is the
# base.  Bush density = tree density × 0.4 (line ~932) × BUSH_DENSITY_MULT.
# Defaults to 1.0 if biome not listed.  Use to bump bush density independently
# of tree density (e.g. KARST should have way more bushes per user feedback).
BUSH_DENSITY_MULT: dict[str, float] = {
    "KARST_BARRENS":           2.5,    # user: way way way more bushes (34,9)
    "DRY_WOODLAND_MAQUIS":     3.0,    # S87w3 was 1.8 — user: MASSIVELY (36,75)
    "DESERT_STEPPE_TRANSITION":1.5,    # user: more short veg + bushes
    "ARCTIC_TUNDRA":           0.5,    # user: sparse bushes but present (33,13)
    "BOREAL_ALPINE":           1.3,    # user: differentiate BA from BT/SBT
}


# S89 walk: ABSOLUTE per-biome bush density target (probability per candidate
# pixel). When present, REPLACES the BASE_DENSITY×0.4×MULT chain for the bush
# pass. Rationale: deserts/scrub have near-zero TREE density, so anchoring bush
# density to tree density (the old BUSH_DENSITY_MULT scheme) can't reach the
# heavy bush fill the user wants ("fill in empty spaces"). These set the bush
# rate directly; placement noise + eco modulation still apply for organic
# variation, and the canopy-radius packing limit still caps actual placements.
# Reference: KARST_BARRENS ≈ 0.70 (the "lots of bushes" benchmark).
BUSH_DENSITY_ABS: dict[str, float] = {
    "KARST_BARRENS":            0.70,  # benchmark
    "DESERT_STEPPE_TRANSITION": 0.65,  # user: ~karst amount, keep trees the same
    "SCRUBBY_HEATHLAND":        0.55,  # user: much closer to karst
    "SEMI_ARID_SHRUBLAND":      0.12,  # S89 walk3: sparser yet (was 0.22)
    "DRY_WOODLAND_MAQUIS":      0.50,  # user: way more generic (non-pine) bush
}

# S98 ISLAND walk: treeless semi-arid shrubland on islands should be SO brushy the
# ground is hidden. Push the bush probability past the KARST 0.70 "lots" benchmark to
# the packing ceiling. Env-gated (VANDIR_ISLAND_RENDER) so mainland semi-arid is untouched.
import os as _os_isl
_SARID_ISLAND_DENSE = bool(_os_isl.environ.get("VANDIR_ISLAND_RENDER"))
if _SARID_ISLAND_DENSE:
    BUSH_DENSITY_ABS["SEMI_ARID_SHRUBLAND"] = 0.95


# ---------------------------------------------------------------------------
# DATA STRUCTURES
# ---------------------------------------------------------------------------

@dataclass
class PlacementRecord:
    """One schematic placement — consumed by chunk_writer (Step 9)."""
    schem_path:   str        # absolute path to .schem file
    world_x:      int        # pixel X in world space
    world_z:      int        # pixel Z in world space
    place_y:      int        # MC Y for schematic origin (bottom of bounding box)
    anchor_y:     int        # Y of trunk base within schematic space
    inset_depth:  int        # mandatory sink depth
    extra_inset:  int        # additional Y-variation sink
    size:         str        # "sm" | "md" | "lg"
    schem_type:   str        # "tree" | "bush"
    biome:        str        # biome name (for debug/logging)
    rotation:     int = 0    # 0-3 = 0°/90°/180°/270° CW rotation in XZ plane
    species:      str = "generic"  # S71-3 species — used for anti-clustering re-roll


@dataclass
class _SchematicEntry:
    """Parsed entry from schematic_index.json."""
    path:            str
    biome:           str
    size:            str
    schem_type:      str
    anchor_y:        int
    inset_depth:     int
    lowest_leaf_y:   int
    method:          str       # trunk_at_bottom | no_trunk_lowest_solid | marker_below_trunk
    weight:          int       # from size code
    anchor_review:   bool
    species:         str = "generic"  # species name for habitat matching


# ---------------------------------------------------------------------------
# INDEX LOADING
# ---------------------------------------------------------------------------

_INDEX_KEY_MAP: dict[str, str] = {
    "alpine":   "SNOWY_BOREAL_TAIGA",  # ALPINE_MEADOW retired S56; S60: BOREAL_ALPINE mirrors SBT via post-load step below
    "birch":    "BIRCH_FOREST",
    "btaiga":   "BOREAL_TAIGA",
    "cheath":   "COASTAL_HEATH",
    "cstep":    "CONTINENTAL_STEPPE",
    "dosav":    "DRY_OAK_SAVANNA",
    "dpine":    "DRY_PINE_BARRENS",
    "dstep":    "DESERT_STEPPE_TRANSITION",
    "etcoast":  "EASTERN_TEMPERATE_COAST",
    "fen":      "FRESHWATER_FEN",
    "heath":    "SCRUBBY_HEATHLAND",
    "karst":    "KARST_BARRENS",
    "lrfc":     "LUSH_RAINFOREST_COAST",
    "mangr":    "MANGROVE_COAST",
    "maquis":   "DRY_WOODLAND_MAQUIS",
    "mixed":    "MIXED_FOREST",
    "rfc":      "RAINFOREST_COAST",
    "ripar":    "RIPARIAN_WOODLAND",
    "sarid":    "SEMI_ARID_SHRUBLAND",
    "sbtaiga":  "SNOWY_BOREAL_TAIGA",
    "tdec":     "TEMPERATE_DECIDUOUS",
    "tjf":      "TIDAL_JUNGLE_FRINGE",
    "train":    "TEMPERATE_RAINFOREST",
}


def _parse_entry(item: dict, biome_code: str) -> _SchematicEntry | None:
    if item.get("anchor_review", False):
        return None
    return _SchematicEntry(
        path          = item["path"],
        biome         = biome_code,
        size          = item.get("size", "sm"),
        schem_type    = item.get("type", "tree"),
        anchor_y      = int(item.get("anchor_y", 0)),
        inset_depth   = int(item.get("inset_depth", 0)),
        lowest_leaf_y = int(item.get("lowest_leaf_y", 0)),
        method        = item.get("method", "trunk_at_bottom"),
        weight        = SIZE_VARIATION.get(item.get("size", "sm"), (1, []))[0],
        anchor_review = False,
        species       = item.get("species", "generic"),
    )


def load_index(index_path: Path) -> dict[str, list[_SchematicEntry]]:
    """
    Load and group schematic_index.json by biome name.
    Handles both list format [{biome: ..., ...}] and dict format {short_key: [entries]}.
    Call once at pipeline startup — not per tile.

    Returns:
        dict mapping biome_code → list of _SchematicEntry
    """
    with open(index_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    grouped: dict[str, list[_SchematicEntry]] = {}
    generic_entries: list[_SchematicEntry] = []

    if isinstance(raw, dict):
        # Dict format: {short_key: [entry, ...]}
        for short_key, items in raw.items():
            if short_key == "generic":
                for item in items:
                    e = _parse_entry(item, "generic")
                    if e:
                        generic_entries.append(e)
                continue
            biome_code = _INDEX_KEY_MAP.get(short_key, short_key.upper())
            for item in items:
                e = _parse_entry(item, biome_code)
                if e:
                    grouped.setdefault(biome_code, []).append(e)
    else:
        # List format: [{biome: ..., ...}]
        for item in raw:
            biome_code = item.get("biome", "")
            if not biome_code:
                continue
            e = _parse_entry(item, biome_code)
            if e:
                grouped.setdefault(biome_code, []).append(e)

    # Merge generic entries into every biome that permits them
    if generic_entries:
        for biome_code in set(grouped.keys()) | (BASE_DENSITY.keys() - NO_BUSH_BIOMES):
            if biome_code not in NO_BUSH_BIOMES:
                grouped.setdefault(biome_code, [])
                for e in generic_entries:
                    grouped[biome_code].append(_SchematicEntry(
                        path=e.path, biome=biome_code, size=e.size,
                        schem_type=e.schem_type, anchor_y=e.anchor_y,
                        inset_depth=e.inset_depth, lowest_leaf_y=e.lowest_leaf_y,
                        method=e.method, weight=e.weight, anchor_review=False,
                        species=e.species,
                    ))

    # S60: BOREAL_ALPINE (zone 40, introduced S58) mirrors SNOWY_BOREAL_TAIGA's
    # full entry set — per-user rule "should match SBT entirely". This covers
    # the high-alpine tier where the schematic_index has no dedicated tree
    # entries; alpine pixels get the same coniferous species as SBT.
    if "SNOWY_BOREAL_TAIGA" in grouped:
        grouped["BOREAL_ALPINE"] = [
            _SchematicEntry(
                path=e.path, biome="BOREAL_ALPINE", size=e.size,
                schem_type=e.schem_type, anchor_y=e.anchor_y,
                inset_depth=e.inset_depth, lowest_leaf_y=e.lowest_leaf_y,
                method=e.method, weight=e.weight, anchor_review=False,
                species=e.species,
            )
            for e in grouped["SNOWY_BOREAL_TAIGA"]
        ]

    # S71-3: GLOBAL leaf-column blacklist — schematics that produce the
    # "column of leaves" bug per user walks (small trees with sparse trunks
    # that get classified bush-like and sunk underground, leaving canopy
    # only).  Removed entirely from the index across all biomes.
    _LEAF_COLUMN_REJECT = (
        "dstep_tree_acacia_a_sm",
        "dstep_tree_acacia_c_sm",
        "dosav_tree_soak_a_sm",
        "dosav_tree_soak_b_sm",
    )
    for _biome_key, _entries in list(grouped.items()):
        grouped[_biome_key] = [
            e for e in _entries
            if not any(rej in e.path for rej in _LEAF_COLUMN_REJECT)
        ]

    # S71-3: SEMI_ARID_SHRUBLAND tree filter — keep only spruce-leaf species.
    # Audited 13 sarid_tree_*.schem files: juniper_a/b/c_sm use acacia_leaves,
    # all other juniper + all pinon use spruce_leaves.  User wants only the
    # spruce-leaf set.  Filter by path substring.
    if "SEMI_ARID_SHRUBLAND" in grouped:
        import os as _os
        if _os.environ.get("VANDIR_ISLAND_RENDER"):
            # S98 ISLAND: semi-arid shrubland is TREELESS + maximally brushy (user) —
            # drop every tree, keep only bushes (density pumped via BUSH_DENSITY_ABS=0.95).
            grouped["SEMI_ARID_SHRUBLAND"] = [
                e for e in grouped["SEMI_ARID_SHRUBLAND"] if e.schem_type != "tree"
            ]
        else:
            # mainland: keep only the spruce-leaf species (drop acacia-leaf juniper).
            _SARID_REJECT = ("sarid_tree_juniper_a_sm", "sarid_tree_juniper_b_sm",
                             "sarid_tree_juniper_c_sm")
            grouped["SEMI_ARID_SHRUBLAND"] = [
                e for e in grouped["SEMI_ARID_SHRUBLAND"]
                if not any(rej in e.path for rej in _SARID_REJECT)
            ]

    # S87: per-biome tree weighting from cross-section walk.
    # Heights are from tools/diag_tree_cross_section.py output (Y dim of bbox).
    # Each section below: drop list + path-stem -> weight overrides.
    # Default weight for unlisted entries within an affected biome stays at
    # SIZE_VARIATION default; only listed stems get overridden.

    # --- BIRCH_FOREST ---
    # dbirch 18-24 dominant; rowan slightly less; sbirch generally less,
    # sbirch_c_sm + largest sbirch (28 blocks) very very rare.
    if "BIRCH_FOREST" in grouped:
        # S89 walk3 REWEIGHT: dbirch + rowan are the small thin "stick"/fence
        # trees (~11 log, ~54 leaves). User wants them OK but RARE. The full
        # leafy sbirch (45-59 log, 400-900 leaves) now DOMINATE; sticks are
        # occasional accents.
        _BIRCH_WEIGHTS = {
            # dbirch (stick/fence): rare accent (was 50)
            "dbirch_a_sm": 10, "dbirch_b_sm": 10, "dbirch_c_sm": 10,
            "dbirch_d_md": 10, "dbirch_e_md": 10, "dbirch_f_lg": 10,
            "dbirch_g_lg": 10,
            # rowan (stick): rare accent (was 25)
            "rowan_a_sm": 6, "rowan_b_sm": 6, "rowan_c_sm": 6,
            # sbirch full leafy birch: NOW DOMINANT (was 10)
            "sbirch_a_sm": 25, "sbirch_d_sm": 45, "sbirch_g_md": 45,
            "sbirch_h_md": 45, "sbirch_i_md": 45, "sbirch_j_md": 45,
            # sbirch taller: common
            "sbirch_e_sm": 15, "sbirch_k_lg": 12,
            # sbirch biggest: less common (variety)
            "sbirch_b_sm": 5, "sbirch_c_sm": 5, "sbirch_f_md": 5,
            "sbirch_l_lg": 5, "sbirch_m_lg": 5,
        }
        for e in grouped["BIRCH_FOREST"]:
            for stem, w in _BIRCH_WEIGHTS.items():
                if stem in e.path:
                    e.weight = w
                    break

    # --- BOREAL_TAIGA ---
    # Bell curve centered 22-26.  bspruce_b_sm explicitly rare per user.
    if "BOREAL_TAIGA" in grouped:
        # S87 walk #3: tighten BT tree range toward SBT (smaller bias).
        # 20-25 block trees down-weighted 30 -> 15 so BT canopy reads as
        # more mid-sized, closer to SBT's "all kept under 20" range but
        # not identical.
        _BTAIGA_WEIGHTS = {
            # 22-26 bell-center: walk#3 down-weighted 30 -> 15
            "bspruce_c_sm": 15, "bspruce_d_sm": 15, "bspruce_e_sm": 15,
            "bspruce_i_md": 15, "bspruce_j_lg": 15, "bspruce_k_lg": 15,
            "bspruce_l_lg": 15,
            # bspruce_b_sm: pretty rare per user
            "bspruce_b_sm": 3,
            # bell-edge low (12-21): rarer
            "balfir_a_sm": 10, "tamarack_a_sm": 5,
            # outer-high (30-35): rare
            "bspruce_a_sm": 5, "bspruce_f_md": 5, "bspruce_g_md": 5,
            "bspruce_h_md": 5, "wspruce_a_sm": 5, "wspruce_b_lg": 5,
            # extreme outlier (40): very rare
            "jpine_a_sm": 1,
        }
        for e in grouped["BOREAL_TAIGA"]:
            for stem, w in _BTAIGA_WEIGHTS.items():
                if stem in e.path:
                    e.weight = w
                    break

    # --- S87 #12 BT/SBT HEIGHT CULL ---
    # User: "higher = smaller trees" rule via explicit height drops.
    # BT drops trees > 25 blocks tall.  SBT drops trees > 20 blocks tall.
    # Heights from tools/diag_tree_cross_section.py output.  Done as a
    # path-stem reject list because schematic_index has no height metadata.
    _BTAIGA_OVER_25_REJECT = (
        "bspruce_a_sm",   # 35 blocks
        "bspruce_b_sm",   # 35 blocks
        "bspruce_c_sm",   # 26 blocks
        "bspruce_e_sm",   # 26 blocks
        "bspruce_f_md",   # 31 blocks
        "bspruce_g_md",   # 33 blocks
        "bspruce_h_md",   # 30 blocks
        "jpine_a_sm",     # 40 blocks
        "wspruce_a_sm",   # 35 blocks
        "wspruce_b_lg",   # 34 blocks
    )
    _SBTAIGA_OVER_20_REJECT = (
        "salfir_a_sm",    # 26 blocks
        "salfir_b_sm",    # 23 blocks
        "salfir_e_sm",    # 21 blocks
        "salfir_f_sm",    # 21 blocks
        "salfir_g_md",    # 21 blocks
        "salfir_i_md",    # 22 blocks
        "salfir_j_md",    # 27 blocks
        "salfir_n_lg",    # 27 blocks
        "spruce_a_sm",    # 36 blocks
        "spruce_b_md",    # 27 blocks
        "spruce_c_lg",    # 21 blocks
    )
    if "BOREAL_TAIGA" in grouped:
        grouped["BOREAL_TAIGA"] = [
            e for e in grouped["BOREAL_TAIGA"]
            if not any(rej in e.path for rej in _BTAIGA_OVER_25_REJECT)
        ]
    if "SNOWY_BOREAL_TAIGA" in grouped:
        grouped["SNOWY_BOREAL_TAIGA"] = [
            e for e in grouped["SNOWY_BOREAL_TAIGA"]
            if not any(rej in e.path for rej in _SBTAIGA_OVER_20_REJECT)
        ]

    # --- SNOWY_BOREAL_TAIGA ---
    # User: currently too tall.  Apply same bell-curve concept as BT but
    # centered LOWER (17-22) to keep "higher = smaller trees" rule.
    if "SNOWY_BOREAL_TAIGA" in grouped:
        _SBTAIGA_WEIGHTS = {
            # 13-22 sweet spot
            "salfir_d_sm": 30, "salfir_l_md": 30, "salfir_m_lg": 30,
            "salfir_p_lg": 30, "salfir_h_md": 30, "salfir_k_md": 30,
            "salfir_o_lg": 30, "spruce_c_lg": 30,
            # 19-22 next-sweet
            "salfir_c_sm": 25, "salfir_g_md": 25, "salfir_i_md": 25,
            "salfir_e_sm": 25, "salfir_f_sm": 25,
            # krummholz 3-11: present but sparse
            "kfir_a_sm": 10, "kfir_b_sm": 10, "kfir_c_sm": 10,
            "kfir_d_sm": 10, "kfir_e_sm": 10, "kfir_f_sm": 10,
            "kfir_g_sm": 10,
            "kspruce_a_sm": 10, "kspruce_b_sm": 10, "kspruce_c_sm": 10,
            # 23-26: less
            "salfir_b_sm": 10,
            # 27 outlier: rare
            "salfir_a_sm": 3, "salfir_j_md": 3, "salfir_n_lg": 3,
            "spruce_b_md": 3,
            # 36 extreme: very rare
            "spruce_a_sm": 1,
        }
        for e in grouped["SNOWY_BOREAL_TAIGA"]:
            for stem, w in _SBTAIGA_WEIGHTS.items():
                if stem in e.path:
                    e.weight = w
                    break

    # --- RAINFOREST_COAST ---
    # User: teak_b/c/d are palms (artifact look) — DROP.  Bias toward teak_a.
    if "RAINFOREST_COAST" in grouped:
        _RFC_DROP = ("teak_b_sm", "teak_c_md", "teak_d_lg")
        grouped["RAINFOREST_COAST"] = [
            e for e in grouped["RAINFOREST_COAST"]
            if not any(rej in e.path for rej in _RFC_DROP)
        ]
        _RFC_WEIGHTS = {
            "teak_a_sm": 30,    # most common
            "kapok_a_lg": 10,
            "tfig_a_sm": 10,
            "tfig_b_lg": 10,
        }
        for e in grouped["RAINFOREST_COAST"]:
            for stem, w in _RFC_WEIGHTS.items():
                if stem in e.path:
                    e.weight = w
                    break

    # --- FRESHWATER_FEN (judgment-based) ---
    # Real fen: alder + cherry willow dominant on waterlogged ground.
    # Heights 13-22 dominant; tiny saplings + oak interloper rare.
    if "FRESHWATER_FEN" in grouped:
        _FEN_WEIGHTS = {
            # alder dominants
            "alder_a_sm": 30, "alder_b_sm": 30, "alder_c_md": 30,
            # cwillow dominants
            "cwillow_b_sm": 30, "cwillow_c_sm": 30, "cwillow_d_md": 30,
            "cwillow_e_md": 30, "cwillow_f_md": 30,
            # alder edge sizes
            "alder_d_md": 10, "alder_e_lg": 10, "alder_f_lg": 10,
            # cwillow large bank trees
            "cwillow_g_lg": 10, "cwillow_h_lg": 10,
            # rare: sapling + interloper
            "cwillow_a_sm": 5,
            "tdec_eoak_a_sm": 5,
        }
        for e in grouped["FRESHWATER_FEN"]:
            for stem, w in _FEN_WEIGHTS.items():
                if stem in e.path:
                    e.weight = w
                    break

    # --- DRY_WOODLAND_MAQUIS (judgment-based) ---
    # Real Mediterranean maquis is SHORT (4-8m IRL).  All our trees are tall
    # for the biome.  Push toward smallest; kill the giant hoak_c_lg.
    if "DRY_WOODLAND_MAQUIS" in grouped:
        _MAQUIS_WEIGHTS = {
            # shortest (12-13): dominant
            "carob_a_sm": 30, "apine_a_sm": 30,
            "carob_b_lg": 20,
            # olive (17-18): medium
            "olive_a_sm": 20, "olive_b_md": 20, "olive_c_lg": 20,
            # hoak (20-21): less
            "hoak_a_sm": 15, "hoak_b_md": 15,
            # hoak_c_lg (29): way too tall for maquis
            "hoak_c_lg": 3,
        }
        for e in grouped["DRY_WOODLAND_MAQUIS"]:
            for stem, w in _MAQUIS_WEIGHTS.items():
                if stem in e.path:
                    e.weight = w
                    break

    # --- MIXED_FOREST (judgment-based) ---
    # Out-of-scale btaiga mirrors (bspruce/wspruce/jpine 30-40 blocks)
    # downplayed.  Birch/lime/eoak at 16-26 are core mixed-temperate.
    if "MIXED_FOREST" in grouped:
        _MIXED_WEIGHTS = {
            # core mixed-temperate trees
            "lime_a_sm": 30, "lime_b_md": 30, "lime_c_lg": 30,
            "eoak_a_sm": 30,
            "sbirch_g_md": 30, "sbirch_h_md": 30, "sbirch_i_md": 30,
            "sbirch_j_md": 30, "sbirch_k_lg": 30, "sbirch_d_sm": 30,
            # outer-high: rare in mixed forest
            "bspruce_a_sm": 5, "bspruce_g_md": 5, "bspruce_h_md": 5,
            "wspruce_a_sm": 5, "wspruce_b_lg": 5,
            # extreme: very rare
            "jpine_a_sm": 1,
        }
        for e in grouped["MIXED_FOREST"]:
            for stem, w in _MIXED_WEIGHTS.items():
                if stem in e.path:
                    e.weight = w
                    break

    # S87: DRY_PINE_BARRENS height-weighted tree palette per user walk.
    # User wants trees 15-26 blocks tall to dominate, smaller + outlier-tall
    # trees much rarer.  scotsp_c_md (27 blocks) explicitly removed.
    # Heights are from tools/diag_tree_cross_section.py output.
    if "DRY_PINE_BARRENS" in grouped:
        _DPINE_DROP = ("scotsp_c_md",)  # removed entirely
        # path-stem -> weight override.  Sweet-spot 15-26 = 30,
        # small (<15) = 5, outlier (>26) = 2.
        _DPINE_WEIGHTS = {
            # SWEET SPOT (heights 15-26) — common
            "ppine_a_sm":   30,  # 16 blocks
            "ppine_b_sm":   30,  # 26 blocks
            "scotsp_b_sm":  30,  # 23 blocks
            "scotsp_d_md":  30,  # 15 blocks
            "scotsp_e_lg":  30,  # 23 blocks
            # SMALL (<15) — rarer
            "pitchp_a_sm":   5,  # 12 blocks
            "pitchp_b_lg":   5,  # 9 blocks
            "ppine_c_sm":    5,  # 11 blocks
            "ppine_g_lg":    5,  # 6 blocks
            "scotsp_a_sm":   5,  # 5 blocks
            # OUTLIER large (>26) — very rare
            "ppine_d_md":    2,  # 45 blocks
            "ppine_e_md":    2,  # 38 blocks
            "ppine_f_lg":    2,  # 34 blocks
        }
        # Drop scotsp_c_md
        grouped["DRY_PINE_BARRENS"] = [
            e for e in grouped["DRY_PINE_BARRENS"]
            if not any(rej in e.path for rej in _DPINE_DROP)
        ]
        # Apply per-species weight overrides
        for e in grouped["DRY_PINE_BARRENS"]:
            for stem, w in _DPINE_WEIGHTS.items():
                if stem in e.path:
                    e.weight = w
                    break

    # S86 Item 3E: DRY_WOODLAND_MAQUIS pine-leaf rarefaction.
    # User feedback (36,75): "For the trees with pine leaves, make them
    # exceedingly rare in this biome".  Maquis currently has 4 apine
    # entries (Aleppo pine) out of 12 tree entries (~33%).  Drop the 3
    # larger sizes (b_sm, c_md, d_lg) and keep only apine_a_sm.  Result:
    # ~8% pine-leaf coverage among maquis trees, dominantly broadleaf
    # (carob/hoak/olive) per Mediterranean palette intent.
    if "DRY_WOODLAND_MAQUIS" in grouped:
        # S98: keep only apine_d (the bushy one) + broadleaf carob/holm-oak/olive;
        # drop the spindly apine a/b/c pines. Global — and on islands the maquis biome
        # is entirely replaced by SEMI_ARID_SHRUBLAND anyway (BANDS 210->200).
        _MAQUIS_PINE_REJECT = (
            "maquis_tree_apine_a_sm",
            "maquis_tree_apine_b_sm",
            "maquis_tree_apine_c_md",
        )
        grouped["DRY_WOODLAND_MAQUIS"] = [
            e for e in grouped["DRY_WOODLAND_MAQUIS"]
            if not any(rej in e.path for rej in _MAQUIS_PINE_REJECT)
        ]

    # S71-3 swap: ARCTIC_TUNDRA + FROZEN_FLATS both mirror SBT size=sm trees
    # (smallest pines).  AT uses these very-very sparsely (×0.05 tree mult);
    # FF uses them sparsely as the "tundra valley" backdrop.  Bushes come
    # from the generic-bush merge (separate path) for both biomes.
    #
    # S85: FF excludes 6 of the 17 size=sm SBT trees that are mature/tall
    # variants mis-labeled as "sm" — actual heights up to 36 blocks (spruce_a)
    # and 19-26 blocks (salfir a-c, e, f).  These read as mature mountain
    # forest trees, NOT tundra-valley krummholz.  After filter FF gets 11
    # entries, all ≤11 blocks tall: kspruce a/b/c + kfir a/c + salfir_d
    # (krummholz) + kfir b/g (saplings) + kfir d/e/f (dwarf saplings).
    # AT keeps the full 17-entry set since its mountain-snowy context can
    # support occasional tall trees + ×0.05 mult makes them rare regardless.
    if "SNOWY_BOREAL_TAIGA" in grouped:
        _all_sm_pines = [
            e for e in grouped["SNOWY_BOREAL_TAIGA"]
            if e.size == "sm" and e.schem_type == "tree"
        ]
        _FF_HEIGHT_REJECT = (
            "sbtaiga_tree_spruce_a_sm",   # 36 blocks — mis-labeled outlier
            "sbtaiga_tree_salfir_a_sm",   # 26 blocks
            "sbtaiga_tree_salfir_b_sm",   # 23 blocks
            "sbtaiga_tree_salfir_c_sm",   # 19 blocks
            "sbtaiga_tree_salfir_e_sm",   # 21 blocks
            "sbtaiga_tree_salfir_f_sm",   # 21 blocks
        )
        _ff_pines = [
            e for e in _all_sm_pines
            if not any(rej in e.path for rej in _FF_HEIGHT_REJECT)
        ]
        for _target, _src in (
            ("ARCTIC_TUNDRA", _all_sm_pines),
            ("FROZEN_FLATS",  _ff_pines),
        ):
            _mirrored = [
                _SchematicEntry(
                    path=e.path, biome=_target, size=e.size,
                    schem_type=e.schem_type, anchor_y=e.anchor_y,
                    inset_depth=e.inset_depth, lowest_leaf_y=e.lowest_leaf_y,
                    method=e.method, weight=e.weight, anchor_review=False,
                    species=e.species,
                )
                for e in _src
            ]
            grouped.setdefault(_target, []).extend(_mirrored)

    # S69: SAND_DUNE_DESERT mirrors KARST_BARRENS bush entries only — gives
    # dunes occasional small-scrub placements (Monahans Sandhills style).
    # Trees are NOT mirrored; combined with SAND_DUNE_DESERT's low
    # BASE_DENSITY (0.008) this yields very rare bush clumps, not a forest.
    if "KARST_BARRENS" in grouped:
        grouped["SAND_DUNE_DESERT"] = [
            _SchematicEntry(
                path=e.path, biome="SAND_DUNE_DESERT", size=e.size,
                schem_type=e.schem_type, anchor_y=e.anchor_y,
                inset_depth=e.inset_depth, lowest_leaf_y=e.lowest_leaf_y,
                method=e.method, weight=e.weight, anchor_review=False,
                species=e.species,
            )
            for e in grouped["KARST_BARRENS"]
            if e.schem_type == "bush"
        ]

    # S89 GLOBAL REJECT: drop bad schematics everywhere by path substring.
    # dpine_tree_scotsp_b_sm uses stripped_dark_oak_log/wood (a light stripped-
    # oak trunk + bundled spruce leaves) -- reads as a broadleaf, not a conifer.
    _GLOBAL_REJECT_STEMS = ("dpine_tree_scotsp_b_sm",)
    if _GLOBAL_REJECT_STEMS:
        for _bk in list(grouped.keys()):
            grouped[_bk] = [e for e in grouped[_bk]
                            if not any(s in e.path for s in _GLOBAL_REJECT_STEMS)]

    return grouped


# ---------------------------------------------------------------------------
# NOISE HELPER
# ---------------------------------------------------------------------------

def _fbm(gen, x: float, y: float, octaves: int = 3) -> float:
    """fBm noise → [0, 1]."""
    val, amp, freq = 0.0, 1.0, 1.0
    for _ in range(octaves):
        val += gen.noise2(x * freq, y * freq) * amp
        amp *= 0.5
        freq *= 2.0
    return max(0.0, min(1.0, (val + 2.0) / 4.0))


# ---------------------------------------------------------------------------
# Y-VARIATION
# ---------------------------------------------------------------------------

def _compute_extra_inset(
    size: str,
    method: str,
    base_y: int,
    lowest_leaf_y: int,
    surface_y: int,
    rng: random.Random,
) -> int:
    """
    Draw extra inset from size-dependent distribution.
    Falls back to 0 if leaf clearance check fails.
    Excluded entirely for method == 'no_trunk_lowest_solid'.
    """
    if method == "no_trunk_lowest_solid":
        return 0

    dist = SIZE_VARIATION[size][1]
    # Weighted draw: [p+0, p+1, p+2]
    r = rng.random()
    extra = 0
    cumulative = 0.0
    for i, p in enumerate(dist):
        cumulative += p
        if r < cumulative:
            extra = i
            break

    if extra == 0:
        return 0

    # Leaf clearance check
    leaf_world_y = base_y + lowest_leaf_y - extra
    if leaf_world_y <= surface_y:
        return 0  # fall back to flush

    return extra


def _compute_extra_inset_u(
    size: str,
    method: str,
    base_y: int,
    lowest_leaf_y: int,
    surface_y: int,
    u: float,
) -> int:
    """RNG-FREE twin of _compute_extra_inset (S96 BUG 1).

    Identical draw logic, but the uniform is the caller-supplied *u* ∈ [0,1)
    instead of rng.random().  The S96 seam rim pass keys *u* on the world
    ANCHOR cell via _wc_u01s so both adjacent tiles compute the SAME extra_inset
    for a shared seam tree — without it the tile-local rng (advanced by a
    different amount on each side) gave the two clipped halves a 1-2 block Y
    step at the seam."""
    if method == "no_trunk_lowest_solid":
        return 0
    dist = SIZE_VARIATION[size][1]
    extra = 0
    cumulative = 0.0
    for i, p in enumerate(dist):
        cumulative += p
        if u < cumulative:
            extra = i
            break
    if extra == 0:
        return 0
    leaf_world_y = base_y + lowest_leaf_y - extra
    if leaf_world_y <= surface_y:
        return 0
    return extra


# ---------------------------------------------------------------------------
# CANOPY EXCLUSION GRID
# ---------------------------------------------------------------------------

class _ExclusionGrid:
    """
    Fast O(1) exclusion check using a 2D occupancy array.
    Marks a radius circle around each placed schematic.
    """

    def __init__(self, h: int, w: int) -> None:
        self._grid = np.zeros((h, w), dtype=bool)
        self._h = h
        self._w = w

    def is_clear(self, row: int, col: int, radius: int) -> bool:
        r0 = max(0, row - radius)
        r1 = min(self._h, row + radius + 1)
        c0 = max(0, col - radius)
        c1 = min(self._w, col + radius + 1)
        return not np.any(self._grid[r0:r1, c0:c1])

    def mark(self, row: int, col: int, radius: int) -> None:
        r0 = max(0, row - radius)
        r1 = min(self._h, row + radius + 1)
        c0 = max(0, col - radius)
        c1 = min(self._w, col + radius + 1)
        # Circle mask
        for r in range(r0, r1):
            for c in range(c0, c1):
                if (r - row) ** 2 + (c - col) ** 2 <= radius * radius:
                    self._grid[r, c] = True


# ---------------------------------------------------------------------------
# PUBLIC API
# ---------------------------------------------------------------------------

def place_schematics(
    surface_y:    np.ndarray,          # (H, W) int16
    biome_grid:   np.ndarray,          # (H, W) object str
    river_meta:   np.ndarray,          # (H, W) uint8
    moisture_tile: np.ndarray,         # (H, W) float32
    noise_fields:  dict,
    cfg:           dict,
    index:         dict[str, list[_SchematicEntry]],
    tile_x:        int,
    tile_y:        int,
    eco_grads=None,                    # Optional EcoGradients from eco_gradients.py
    cliff_deg:    np.ndarray | None = None,  # (H,W) float32 degrees
    clearing_field: np.ndarray | None = None,  # (H,W) float32 [0,1] — meadow clearing noise (S57 Phase 3a)
    surface_blocks: np.ndarray | None = None,  # (H,W) object str — for snow-surface skip (S58)
    cliff_cap_tile: np.ndarray | None = None,  # (H,W) float32 [0,1] — walk #12: suppress trees on cap
    biome_grid_padded: np.ndarray | None = None,  # (H+2p,W+2p) object str — S93c cross-tile ecotone halo
    surface_y_padded: np.ndarray | None = None,  # (H+2sp,W+2sp) int — S96 seam rim pass surface halo
    seam_pad_px: int = 0,                         # half-pad width of surface_y_padded
    surface_y_precarve_padded: np.ndarray | None = None,  # (H+2sp,W+2sp) PRE-CARVE LUT surface — S96 BUG2 symmetric place_y
) -> list[PlacementRecord]:
    """
    Compute schematic placements for one tile.

    When *eco_grads* and *cliff_deg* are provided, two ecological enhancements
    activate:
      A. **Eco-modulated density** — tree density scales with terrain (denser in
         moist basins, sparser on ridges/steep slopes).
      B. **Habitat-weighted selection** — species are weighted by terrain match
         via per-species sigmoid preference curves from ``species_habitats`` config.

    Falls back to flat density × uniform random when eco_grads is None.

    Returns a list of PlacementRecord instances ready for chunk_writer.
    """
    H, W = surface_y.shape
    px_off = tile_x * W
    py_off = tile_y * H

    den_cfg = cfg.get("decoration_density_noise", {
        "scale": 60, "octaves": 3, "floor": 0.15,
    })
    den_gen  = noise_fields["decoration_density"]
    den_floor = den_cfg.get("floor", 0.15)
    den_scale = den_cfg.get("scale", 60)
    den_oct   = den_cfg.get("octaves", 3)

    # Seeded RNG — deterministic per tile
    tile_seed = tile_x * 73856093 ^ tile_y * 19349663 ^ GLOBAL_SEED
    rng = random.Random(tile_seed)
    np_rng = np.random.default_rng(tile_seed)

    # Candidate pixel mask: land only, no river/lake/ocean banks, 14-block buffer
    # S65: widen buffer 8→14 per user.  Combined with the whole-schematic
    # reject in chunk_writer.stamp_schematic, gives ~16-20 block no-tree-zone
    # from any water edge.
    from scipy.ndimage import binary_dilation as _bd_place
    water_pixels = (river_meta > 0) | (surface_y < 63)
    water_buffer = _bd_place(water_pixels, iterations=14) if water_pixels.any() else water_pixels
    land_mask = (surface_y >= 63) & ~water_buffer

    # S70 Item M: distance-from-water field, blocks-at-50k.  Used to gate
    # palm species (rfpalm, mpalm, cpalm) in LUSH_RAINFOREST_COAST so palms
    # only fire within 32 blocks of ocean / lake / river edge.
    from scipy.ndimage import distance_transform_edt as _dt_water
    if water_pixels.any():
        dist_to_water_blocks = _dt_water(~water_pixels).astype(np.float32)
    else:
        # No water in tile — anywhere is "far from water"
        dist_to_water_blocks = np.full((H, W), 1e6, dtype=np.float32)

    # S70 Item N: karst bush clustering noise.  Modulates bush density
    # for KARST_BARRENS so bushes form groves rather than evenly-distributed
    # sprinkle.  Scale 60 blocks; output [0.3, 1.7] multiplier (mean 1.0).
    try:
        import opensimplex as _ox_karst_mod
        # S91 regression #2: seed must be GLOBAL (was tile_seed ^ 0xCA757 — a
        # per-tile seed under world coords = a different simplex field per
        # tile, so the grove pattern broke at every KARST tile border).
        _ox_karst = _ox_karst_mod.OpenSimplex(seed=(GLOBAL_SEED ^ 0xCA757) & 0x7FFFFFFF)
        _kx = ((np.arange(W) + px_off) / 60.0).astype(np.float64)
        _kz = ((np.arange(H) + py_off) / 60.0).astype(np.float64)
        _karst_noise = _ox_karst.noise2array(_kx, _kz).astype(np.float32)  # [-1, 1]
        # S70-f4: widened range [0.0, 2.5] for sharper grove vs sparse contrast
        # (was [0.3, 1.7]).  User wants more density in clusters AND more
        # sparse outside them.  Mean density also bumps from 1.0 -> 1.25.
        karst_density_mult = (0.0 + 2.5 * (_karst_noise * 0.5 + 0.5)).astype(np.float32)
        del _kx, _kz, _karst_noise
    except ImportError:
        karst_density_mult = np.ones((H, W), dtype=np.float32)

    # Suppress trees in clearing gaps (gap_mask from eco_gradients)
    if eco_grads is not None and hasattr(eco_grads, 'gap_mask'):
        gap = eco_grads.gap_mask
        # Full suppression: meadow, windthrow, floodplain, bare rock.
        # S71: gap==8 (sand_dune) lifted within SAND_DUNE_DESERT so the biome's
        # mirrored-from-KARST bush entries can fire sparsely on dune surface.
        # S71 walk #3: gap==7 (snow_caps) ALSO lifted within ARCTIC_TUNDRA so
        # the SBT-mirrored small-pine schematics + KARST-style bushes can fire.
        # Trees still won't fire on snow surfaces (the _SNOW_SURFACE_BLOCKS
        # mask in §S58 below catches that); only bushes survive in practice.
        sand_dune_in_desert = (gap == 8) & (biome_grid == "SAND_DUNE_DESERT")
        snow_in_arctic = (gap == 7) & (biome_grid == "ARCTIC_TUNDRA")
        # S86: LUSH biomes survive on floodplain (gap==4).  Riparian woodland
        # in particular is *defined* by flood-adjacency — suppressing schematics
        # there breaks the biome's identity.  Verified at tile (80,50).
        _FLOODPLAIN_OK_BIOMES = (
            "LUSH_RAINFOREST_COAST",
            "RIPARIAN_WOODLAND",
            "FRESHWATER_FEN",
            "TIDAL_JUNGLE_FRINGE",
            "MANGROVE_COAST",
        )
        floodplain_ok = (gap == 4) & np.isin(biome_grid, list(_FLOODPLAIN_OK_BIOMES))
        # S89: DARK-tier rock (gap==5 but slope < t2, ~40-45 deg) keeps SPARSE
        # trees -- only mid/light rock (45 deg+) is bare. Fixes "no trees on
        # slopes in dither zones": the new 40-deg rock cutoff was hard-killing
        # trees on moderately-steep slopes that previously held them. Density on
        # the surviving dark-tier pixels is already thinned by the 30->55 slope
        # penalty downstream, so they read as sparse, not forested.
        _rl_on_sp = bool(cfg.get("lithology", {}).get("rock_layers", {}).get("enabled", False)) if isinstance(cfg, dict) else False
        # S95: islands derive a true-monotone slope -> a BROAD 32-45deg rock band, so the
        # S89 dark-tier exemption (below) leaves ~21% of trees on bare rock. When the
        # island flag is set, suppress ALL gap==5 trees (pre-S89 mainland behavior).
        # Mainland (flag unset) keeps the dark-tier sparse-tree exemption byte-for-byte.
        _all_gap5_off = bool(cfg.get("lithology", {}).get("rock_layers", {}).get("suppress_all_gap5_trees", False)) if isinstance(cfg, dict) else False
        _rock_sup = (gap == 5)
        if _rl_on_sp and cliff_deg is not None and not _all_gap5_off:
            _t2_sp = float(cfg.get("lithology", {}).get("rock_layers", {}).get("t2_deg", 45.0))
            _rock_sup = (gap == 5) & (cliff_deg >= _t2_sp)
        # S95-T2 #5b: islands paint a WIDE beach from beach.tif (gap==9,
        # max_surface_y=80). Unlike the mainland's thin Y64/65 band — which keeps
        # trees off via the 14px water buffer because the whole beach sits inside
        # it — the island sand escapes the buffer, so trees land ON the sand. When
        # the island beach.tif path is active (eco_gradients.beach_gap.from_mask),
        # suppress trees on gap==9. Mainland (flag unset) keeps buffer behavior.
        _beach_from_mask = bool(cfg.get("eco_gradients", {}).get("beach_gap", {}).get("from_mask", False)) if isinstance(cfg, dict) else False
        _beach_sup = (gap == 9) if _beach_from_mask else np.zeros(gap.shape, dtype=bool)
        full_suppress = (
            ((gap == 1) | (gap == 2) | (gap == 4) | _rock_sup
             | (gap == 7) | (gap == 8) | _beach_sup)
            & ~sand_dune_in_desert & ~snow_in_arctic & ~floodplain_ok
        )
        # Walk #12: also suppress trees on cliff_cap pixels (bare peak tops
        # shouldn't have trees growing on them).
        # S89: the suppression zone MUST match the PAINTED cap extent, which the
        # painter (_apply_cliff_cap) widens two ways the raw threshold misses:
        #   (a) cap_dither (+/-N byte) jitters the threshold, so pixels down to
        #       (threshold - cap_dither) get painted -> lower the suppression
        #       threshold by the same margin.
        #   (b) edge_fade_blocks adds a soft ring beyond dilate_blocks -> dilate
        #       the suppression by dilate_blocks + edge_fade_blocks.
        # Without this, trees grew on the cap's ragged dithered edge + fade ring.
        _cc_cfg = cfg.get("lithology", {}).get("cliff_cap", {}) if isinstance(cfg, dict) else {}
        if cliff_cap_tile is not None and _cc_cfg.get("suppress_trees", False):
            _cc_dither = int(round(float(_cc_cfg.get("cap_dither", 0.0))))
            _cc_thr = int(_cc_cfg.get("intensity_threshold", 8)) - _cc_dither
            _cc_dil = int(_cc_cfg.get("dilate_blocks", 0)) + int(round(float(_cc_cfg.get("edge_fade_blocks", 0))))
            _cc_intensity_byte = (cliff_cap_tile * 255.0).astype(np.int32)
            _cap_pixels = _cc_intensity_byte >= _cc_thr
            if _cc_dil > 0 and _cap_pixels.any():
                from scipy.ndimage import binary_dilation as _bd_cc
                _cap_pixels = _bd_cc(_cap_pixels, iterations=_cc_dil)
            full_suppress = full_suppress | _cap_pixels
        land_mask = land_mask & ~full_suppress

        # Alpine meadow (gap==6): allow sparse scattered krummholz trees
        # — don't suppress from land_mask, handled via density below

    # S57 Phase 3a: Suppress trees in meadow clearing interior (biome-gated).
    # Clearing edge thinning is applied as a density modifier below, not a
    # hard suppression.
    _CLEARING_BIOMES_TREE = frozenset({
        "TEMPERATE_RAINFOREST", "TEMPERATE_DECIDUOUS", "BOREAL_TAIGA",
        "MIXED_FOREST", "BIRCH_FOREST", "RIPARIAN_WOODLAND",
    })
    _clearing_interior_px = None
    _clearing_seam_px = None
    if clearing_field is not None:
        from core.meadow_clearing_field import (
            CLEARING_INTERIOR_THRESHOLD as _CF_THR_TR,
            CLEARING_EDGE_BAND as _CF_BAND_TR,
        )
        _cltr_biome = np.zeros((H, W), dtype=bool)
        for _cb in _CLEARING_BIOMES_TREE:
            _cltr_biome |= (biome_grid == _cb)
        if _cltr_biome.any():
            _clearing_interior_px = _cltr_biome & (clearing_field < _CF_THR_TR)
            _clearing_seam_px = _cltr_biome & (np.abs(clearing_field - _CF_THR_TR) < _CF_BAND_TR) & ~_clearing_interior_px
            if _clearing_interior_px.any():
                land_mask = land_mask & ~_clearing_interior_px

        # Edge transitions with noisy dither into forest
        # Windthrow/meadow: wider ragged edge (6px, noise-driven suppression)
        gap_soft = (gap == 1) | (gap == 2)  # meadow, windthrow
        gap_sharp = (gap == 4) | (gap == 5) | (gap == 7) | (gap == 8)  # floodplain, rock, snow, sand
        if gap_soft.any():
            # 6px edge zone, suppression probability varies with world-space noise
            edge_nf = _bd_place(gap_soft, iterations=6) & ~(gap > 0)
            if edge_nf.any():
                from scipy.ndimage import distance_transform_edt as _edt_edge
                dist_from_gap = _edt_edge(~gap_soft).astype(np.float32)
                # Closer to gap = higher suppression (0.7 at gap edge, 0.1 at 6px out)
                suppress_prob = np.clip(0.7 - (dist_from_gap - 1) * 0.1, 0.1, 0.7)
                land_mask = land_mask & ~(edge_nf & (np_rng.random((H, W)) < suppress_prob))
        if gap_sharp.any():
            edge_sh = _bd_place(gap_sharp, iterations=2) & ~(gap > 0)
            land_mask = land_mask & ~(edge_sh & (np_rng.random((H, W)) < 0.7))

    # Build decoration noise tile (vectorised via noise2array)
    try:
        import opensimplex as ox
        base_seed = getattr(den_gen, '_seed', 42002)
        xs_base = (np.arange(W, dtype=np.float64) + px_off) / den_scale
        ys_base = (np.arange(H, dtype=np.float64) + py_off) / den_scale
        accumulated = np.zeros((H, W), dtype=np.float64)
        amplitude, freq, persistence, lacunarity = 1.0, 1.0, 0.5, 2.0
        for octave in range(den_oct):
            ox.seed(base_seed + octave * 7919)
            accumulated += ox.noise2array(xs_base * freq, ys_base * freq) * amplitude
            amplitude *= persistence
            freq *= lacunarity
        noise_tile = accumulated.astype(np.float32)
        # S91 regression #2: FIXED normalization range (was per-tile min/max).
        # The raw fBm is world-continuous (global seed + world coords), but
        # per-tile min-max rescaling made density_mult discontinuous at every
        # tile border (measured ~1.4x interior variation at a typical seam —
        # mild, but world-wide and worse wherever tile extrema differ).
        # +/-1.4 covers the observed per-tile range (~ +/-1.3) for 3-octave
        # persistence-0.5 fBm; clip handles the tail.
        _FBM_RANGE = 1.4
        noise_tile = np.clip(
            (noise_tile + _FBM_RANGE) / (2.0 * _FBM_RANGE), 0.0, 1.0)
    except ImportError:
        noise_tile = np.empty((H, W), dtype=np.float32)
        for row in range(H):
            wy = (py_off + row) / den_scale
            for col in range(W):
                wx = (px_off + col) / den_scale
                noise_tile[row, col] = _fbm(den_gen, wx, wy, den_oct)

    density_mult = den_floor + (1.0 - den_floor) * noise_tile  # [floor, 1.0]

    # ── Stage A: Eco-modulated density ────────────────────────────────────
    eco_density_tile = None
    if eco_grads is not None and cliff_deg is not None:
        ep_cfg = cfg.get("eco_placement", {})
        dw = ep_cfg.get("eco_density_weights",
                        {"soil_depth": 0.3, "moisture_index": 0.4, "concavity_norm": 0.3})
        d_range = ep_cfg.get("eco_density_range", [0.5, 1.5])
        slope_start = float(ep_cfg.get("slope_penalty_start_deg",
                            cfg.get("eco_vegetation", {}).get("slope_penalty_start_deg", 30)))
        slope_full  = float(ep_cfg.get("slope_penalty_full_deg",
                            cfg.get("eco_vegetation", {}).get("slope_penalty_full_deg", 55)))

        # Composite eco density modifier [0, 1]
        eco_mod = (float(dw.get("soil_depth", 0.3))     * eco_grads.soil_depth
                 + float(dw.get("moisture_index", 0.4))  * eco_grads.moisture_index
                 + float(dw.get("concavity_norm", 0.3))  * eco_grads.concavity_norm)
        eco_mod = np.clip(eco_mod, 0.0, 1.0)

        # Map to density range [d_range[0], d_range[1]]
        d_lo, d_hi = float(d_range[0]), float(d_range[1])
        eco_density_tile = d_lo + (d_hi - d_lo) * eco_mod  # (H, W) float32

        # Slope penalty: linear ramp from 1.0 at slope_start to 0.0 at slope_full
        slope_span = max(slope_full - slope_start, 1.0)
        slope_penalty = np.clip(1.0 - (cliff_deg - slope_start) / slope_span, 0.0, 1.0)
        eco_density_tile = eco_density_tile * slope_penalty

    # ── Rock exposure gradient thinning ────────────────────────────────────
    # Trees thin smoothly from 100% at gradient=0 to ~5% at gradient=0.3
    # (alpine meadow threshold).  Above 0.3 is gap_mask==6 (alpine meadow)
    # where only sparse krummholz survives, and above 0.7 is gap_mask==5
    # (bare rock, fully suppressed above).
    # S89: when rock_layers drives rock (gap==5 = slope tier>=1), the OLD Gaea
    # rock_exposure_gradient is a stale, WIDER footprint that wrongly thinned
    # trees to 5% on slopes the new system doesn't call rocky -> "trees not
    # generating on slopes where they should be". Rock tree-exclusion is now
    # fully handled by gap==5 (hard stop) + the slope penalty (30->55 fade).
    _rl_on = bool(cfg.get("lithology", {}).get("rock_layers", {}).get("enabled", False)) if isinstance(cfg, dict) else False
    if (not _rl_on) and eco_grads is not None and hasattr(eco_grads, 'rock_exposure_gradient'):
        re_grad = eco_grads.rock_exposure_gradient
        if re_grad.max() > 0.01:
            # Subalpine thinning: 1.0 at grad=0, 0.05 at grad=0.3
            tree_thin = np.clip(1.0 - re_grad / 0.3, 0.05, 1.0).astype(np.float32)
            # Alpine meadow (gap==6): very sparse krummholz (10% of normal)
            if hasattr(eco_grads, 'gap_mask'):
                tree_thin[eco_grads.gap_mask == 6] = 0.10
            if eco_density_tile is not None:
                eco_density_tile = eco_density_tile * tree_thin
            else:
                eco_density_tile = tree_thin

    # ── S58 Per-biome treeline density modifier ─────────────────────────
    # Linear fade-out of tree density above per-biome y_top, fully zero by
    # y_top + fade_blocks. Stored in cfg.treelines so each biome can have
    # its own treeline (alpine biomes lower, valley biomes higher).
    treelines_cfg = cfg.get("treelines", {}) if isinstance(cfg, dict) else {}
    if treelines_cfg:
        # Build per-pixel y_top + fade arrays from biome lookup.
        _default = treelines_cfg.get("_default", {"y_top": 530, "fade_blocks": 100})
        y_top_tile = np.full((H, W), float(_default.get("y_top", 230)), dtype=np.float32)
        fade_tile  = np.full((H, W), float(_default.get("fade_blocks", 30)), dtype=np.float32)
        for biome, entry in treelines_cfg.items():
            if biome == "_default" or not isinstance(entry, dict):
                continue
            mask = (biome_grid == biome)
            if mask.any():
                y_top_tile[mask] = float(entry.get("y_top", _default.get("y_top", 230)))
                fade_tile[mask]  = float(entry.get("fade_blocks", _default.get("fade_blocks", 30)))
        # Compute linear-ramp density mult: 1.0 below y_top, 0.0 above y_top+fade.
        sy_f = surface_y.astype(np.float32)
        treeline_mult = np.clip(1.0 - (sy_f - y_top_tile) / np.maximum(fade_tile, 1.0), 0.0, 1.0).astype(np.float32)
        if eco_density_tile is not None:
            eco_density_tile = eco_density_tile * treeline_mult
        else:
            eco_density_tile = treeline_mult
        del y_top_tile, fade_tile, treeline_mult

    # ── S58 No trees on snow surface ───────────────────────────────────
    # Skip placement when the surface block at the candidate pixel is one
    # of MC's snow / ice variants. Trees placed on snow_layer or snow_block
    # appear floating because the schematic anchors at the snow surface
    # rather than the buried ground beneath. This applies even after the
    # zone-40 → BOREAL_ALPINE remap eliminates most natural snow.
    # S84: Removed snow_block from skip set so trees can place on snow ground
    # in BOREAL_TAIGA / SNOWY_BOREAL_TAIGA / BOREAL_ALPINE (was zero trees
    # there because Gaea dusting painted snow_block over the whole biome
    # surface). Keep "snow" (1-block snow layer overlay) skipped — trees on
    # that look bad. powder_snow stays skipped (pest block; also removed at
    # source elsewhere in S84). Ice variants still skipped.
    # S89: snow_block RE-ADDED to the skip set. S84 removed it because the OLD
    # Gaea dusting painted snow_block over the WHOLE biome (-> zero trees), but
    # depth-snow now puts snow_block ONLY on CAPS (high-potential summits) while
    # drifts keep the podzol/grass surface (snow is ground_cover). So skipping
    # snow_block removes trees from caps (fixes floating-on-cap) WITHOUT nuking
    # the forest -- trees still place on the non-cap majority.
    _SNOW_SURFACE_BLOCKS = frozenset({
        "snow", "snow_block", "powder_snow", "ice", "packed_ice", "blue_ice",
    })
    snow_surface_mask = None
    if surface_blocks is not None:
        snow_surface_mask = np.zeros((H, W), dtype=bool)
        for _blk in _SNOW_SURFACE_BLOCKS:
            snow_surface_mask |= (surface_blocks == _blk)
        # S71-3: snow-surface exception for AT + FF — both biomes have heavy
        # snow_block surface, but should still allow rare smallest-pine
        # schematics + sparse bushes.  Without this, ~50%+ snow_block surface
        # kicks all schematics off these tiles.
        snow_surface_mask = (snow_surface_mask
                             & (biome_grid != "ARCTIC_TUNDRA")
                             & (biome_grid != "FROZEN_FLATS"))
        # S89 walk3: SNOWY_BOREAL_TAIGA keeps a SPARSE snowy treeline on its high
        # snow-covered massifs. Without this the snow_block surface excludes ALL
        # trees and the high SBT tile reads as ZERO trees. Allow SBT trees on
        # snow on low-to-moderate slopes; krummholz density_mult (0.2) keeps the
        # count a sparse scatter (NOT the old carpet -- that was a density bug,
        # not this exemption), and chunk_writer sink/reject roots them on the
        # snow (no floaters). Steep caps still reject. Snow LAYERS only, not ice.
        if cliff_deg is not None:
            _sbt_max_slope = float(cfg.get("eco_placement", {}).get(
                "sbt_snow_tree_max_slope_deg", 30.0))
            _sbt_snow_ok = (
                (biome_grid == "SNOWY_BOREAL_TAIGA")
                & ((surface_blocks == "snow_block") | (surface_blocks == "snow"))
                & (cliff_deg <= _sbt_max_slope))
            snow_surface_mask = snow_surface_mask & ~_sbt_snow_ok
        if snow_surface_mask.any():
            land_mask = land_mask & ~snow_surface_mask

    # S57 Phase 3a: Clearing seam tree thinning (~40% of normal density).
    # Creates the "scattered trees spilling into clearing" look.  Interior is
    # already fully suppressed via land_mask above.
    if _clearing_seam_px is not None and _clearing_seam_px.any():
        _seam_thin = np.ones((H, W), dtype=np.float32)
        _seam_thin[_clearing_seam_px] = 0.40
        if eco_density_tile is not None:
            eco_density_tile = eco_density_tile * _seam_thin
        else:
            eco_density_tile = _seam_thin
        del _seam_thin

    # ── Stage B: Pre-compute species habitat score maps ───────────────────
    habitats_cfg = cfg.get("species_habitats", {})
    score_floor  = float(cfg.get("eco_placement", {}).get("habitat_score_floor", 0.05))
    species_scores: dict[str, np.ndarray] = {}  # species_name → (H,W) float32

    if eco_grads is not None and cliff_deg is not None and habitats_cfg:
        # Collect unique species across all biomes present in tile
        all_species = set()
        for biome in np.unique(biome_grid):
            entries = index.get(str(biome), [])
            for e in entries:
                if e.species != "generic":
                    all_species.add(e.species)

        # Build gradient arrays dict for vectorized scoring
        grad_arrays = {
            "moisture_index":     eco_grads.moisture_index,
            "soil_depth":         eco_grads.soil_depth,
            "wind_exposure":      eco_grads.wind_exposure,
            "concavity_norm":     eco_grads.concavity_norm,
            "riparian_proximity": eco_grads.riparian_proximity,
        }

        for sp in all_species:
            profile = habitats_cfg.get(sp)
            if profile is None:
                species_scores[sp] = np.ones((H, W), dtype=np.float32)
                continue

            score = np.ones((H, W), dtype=np.float32)
            for grad_name, arr in grad_arrays.items():
                pref = profile.get(grad_name)
                if pref is None:
                    continue
                center, width = float(pref[0]), float(pref[1])
                x = np.clip(-(arr - center) / max(width, 1e-6), -20.0, 20.0)
                score *= (1.0 / (1.0 + np.exp(x))).astype(np.float32)

            # Hard slope cutoff
            max_slope = float(profile.get("cliff_deg_max", 90.0))
            score[cliff_deg > max_slope] = 0.0

            species_scores[sp] = score

    # ── S84: PALM COAST-DISTANCE GATE ────────────────────────────────────
    # Real palms grow within ~50m of tropical coastlines. Zero out palm
    # species scores beyond PALM_MAX_COAST_BLOCKS from the nearest ocean.
    # Without this, palms could spawn anywhere their MC-jungle biome paint
    # exists (which can include inland jungle patches).
    _PALM_SPECIES = {"mpalm", "rfpalm", "cpalm"}
    _PALM_MAX_COAST_BLOCKS = int(cfg.get("eco_placement", {}).get(
        "palm_coast_max_blocks", 30))
    _palms_present = _PALM_SPECIES & set(species_scores.keys())
    if _palms_present:
        from scipy.ndimage import distance_transform_edt as _edt_coast
        SEA_Y = 63
        _ocean_mask = surface_y < SEA_Y
        if _ocean_mask.any() and not _ocean_mask.all():
            _dist_from_ocean = _edt_coast(~_ocean_mask).astype(np.float32)
            _far_inland = _dist_from_ocean > _PALM_MAX_COAST_BLOCKS
            for sp in _palms_present:
                species_scores[sp][_far_inland] = 0.0
            del _dist_from_ocean, _far_inland
        elif not _ocean_mask.any():
            # No ocean in this tile — palms can't spawn (pure-inland)
            for sp in _palms_present:
                species_scores[sp][:] = 0.0
        del _ocean_mask

    # ── Two-pass pixel iteration: trees first, then bushes ──────────────
    # Pass 1 places trees with full canopy exclusion.
    # Pass 2 places bushes with smaller exclusion, allowed near trees.
    exclusion = _ExclusionGrid(H, W)
    placements: list[PlacementRecord] = []

    rows_arr, cols_arr = np.where(land_mask)
    order = np_rng.permutation(len(rows_arr))

    # S59: Ecotone seam dither for schematic placement.
    # Precompute per-pixel swap mask + neighbour biome using the same
    # geometry as the surface/GC dither (30-block linear ramp, 0.5 cap).
    # At rolled candidates, override biome_str to the neighbour biome so the
    # entries list swaps — mixes tree species at biome seams. Independent coin
    # from surface/sub (seed 0xEC0D17E) and GC (0x9C0DEC0); uses 0x5C0DEC0
    # for "schematic ecotone".
    # S93c: when biome_grid_padded is provided, the swap GEOMETRY comes from
    # the padded grid (cropped to inner) — boundaries sitting across a tile
    # seam now project their species mixing into this tile (user report:
    # tree swaps stopped dead at tile borders). The coin stays inner-shaped
    # with the same seed, so interior candidates are byte-identical; the
    # swapped entries list works for halo-only biomes too (index is global).
    _seam_has_nb: np.ndarray | None = None
    _seam_nb: np.ndarray | None = None
    _seam_swap_grid: np.ndarray | None = None
    try:
        from core.surface_decorator import _compute_ecotone_swap_fields as _ecotone_fields
        _pgrid = biome_grid_padded
        _pad_ok = (
            _pgrid is not None
            and getattr(_pgrid, "ndim", 0) == 2
            and _pgrid.shape[0] > H
            and _pgrid.shape[1] > W
            and (_pgrid.shape[0] - H) == (_pgrid.shape[1] - W)
            and (_pgrid.shape[0] - H) % 2 == 0
        )
        if _pad_ok:
            _fields = _ecotone_fields(_pgrid, cfg, gap_mask=None, noise_b=None)
        else:
            _fields = _ecotone_fields(biome_grid, cfg, gap_mask=None, noise_b=None)
        if _fields is not None:
            _seam_has_nb, _seam_nb, _seam_sp, _, _, _ = _fields
            if _pad_ok:
                _p = (_pgrid.shape[0] - H) // 2
                _crop = (slice(_p, _p + H), slice(_p, _p + W))
                _seam_has_nb = _seam_has_nb[_crop]
                _seam_nb = _seam_nb[_crop]
                _seam_sp = _seam_sp[_crop]
            _seam_rng = np.random.default_rng(tile_seed ^ 0x5C0DEC0)
            _seam_coin = _seam_rng.random((H, W)).astype(np.float32)
            _seam_swap_grid = _seam_coin < _seam_sp
    except Exception:
        # Dither is best-effort — failure doesn't break placement.
        _seam_swap_grid = None


    # Split index into trees and bushes per biome
    def _filter_entries(entries, schem_type):
        return [e for e in entries if e.schem_type == schem_type]

    # S86 Item 1D: per-pass rotation tracker keyed by (cell_row, cell_col)
    # at 4-block grid resolution.  Used to enforce rotation variety among
    # adjacent schematic placements (8-neighborhood).  Both trees and bushes
    # share the same tracker so cross-type clusters also get varied rotations.
    _rotation_grid: dict[tuple[int, int], list[int]] = {}

    # S91 regression #2: path -> max(X,Z) extent cache for the tile-boundary
    # clip rejection below (loaded lazily, edge-strip candidates only).
    _schem_extent_cache: dict[str, int] = {}

    # S96 tree-canopy SEAM fix.  The interior `for idx in order` loop — INCLUDING
    # the S91/S92 edge-guard/substitute — is UNCHANGED and runs identically
    # regardless of tree_seam (gating it would shift the interior rng stream =
    # the S91 "forests RE-ROLL" mainland regression).  The fix is a PURE
    # POST-LOOP ADDITION: a world-coord DETERMINISTIC rim pass (below, gated on
    # _seam_on AND surface_y_padded) that fills each tile's LOW edge so the seam
    # canopy meets.  When _seam_on is False (or no padded surface, e.g. the
    # validators), the rim pass is skipped and behaviour is exactly pre-S96.
    _seam_cfg = cfg.get("tree_seam", {}) if isinstance(cfg, dict) else {}
    _seam_on = bool(_seam_cfg.get("enabled", True))

    # S89 krummholz: stunted trees on/near rock.  Where rock_gap (gap==5) fires
    # on or within rock_dilate_blocks of a TREE candidate, OR at/above
    # min_elevation_y, restrict selection to the smallest (size=="sm") variants
    # so high mountain trees cap small near the rock — preserving the COUNT
    # (we still place a tree, just a small one).
    _kr_cfg = cfg.get("krummholz", {}) if isinstance(cfg, dict) else {}
    _kr_on = bool(_kr_cfg.get("enabled", False))
    # Altitude FEATHER: P(force small) ramps 0 at feather_lo_y -> 1 at feather_hi_y
    # (rock proximity is always 1.0). Gives a gradual krummholz band instead of a
    # hard altitude cutoff.
    _kr_feather_lo = float(_kr_cfg.get("feather_lo_y", 550.0))
    _kr_feather_hi = float(_kr_cfg.get("feather_hi_y", 600.0))
    _kr_rock_near = None
    if _kr_on and eco_grads is not None and hasattr(eco_grads, "gap_mask"):
        _kr_rg = (eco_grads.gap_mask == 5)
        if _kr_rg.any():
            from scipy.ndimage import binary_dilation as _bd_kr
            _kr_rock_near = _bd_kr(
                _kr_rg, iterations=int(_kr_cfg.get("rock_dilate_blocks", 3)))
        else:
            _kr_rock_near = _kr_rg
    # S89: SNOW-CAP proximity -> krummholz. Trees rendering within snowcap_dilate
    # blocks of a snow_block cap (i.e. at the upper treeline, against the snow)
    # become the tiny krummholz pines -- stunted trees clinging below the snow.
    _kr_snow_near = None
    if _kr_on and surface_blocks is not None:
        _kr_sc = (surface_blocks == "snow_block")
        if _kr_sc.any():
            from scipy.ndimage import binary_dilation as _bd_krs
            _kr_snow_near = _bd_krs(
                _kr_sc, iterations=int(_kr_cfg.get("snowcap_dilate_blocks", 30)))
    # Krummholz uses a FIXED pool of specific tiny-pine schematics (user-picked),
    # forced at triggered cells regardless of biome. Built by scanning the whole
    # index for matching path stems (dedup by path).
    _kr_whitelist = list(_kr_cfg.get("schematics", []))
    _kr_max_h = int(_kr_cfg.get("max_height_blocks", 10))
    # S89 walk: density multiplier applied to krummholz cells so the band is
    # SPARSE (stunted treeline scatter) rather than a full-density tiny-pine fill.
    _kr_density_mult = float(_kr_cfg.get("density_mult", 0.2))
    # S89 walk3: krummholz density FADES from ~regular-SBT density at the lower
    # treeline edge down to FROZEN_FLATS sparseness over `fade_blocks`, measured
    # as distance INTO the krummholz zone. So the band thins gradually (WAY
    # sparser high up) instead of a uniform mult. far_density_mult ~= FF's
    # effective rate (FF BASE 0.04 * 0.03 ~= 0.0012 vs SBT BASE 1.0).
    _kr_fade_blocks = float(_kr_cfg.get("fade_blocks", 30.0))
    _kr_near = float(_kr_cfg.get("near_density_mult", 1.0))
    _kr_far = float(_kr_cfg.get("far_density_mult", 0.0015))
    # S89-walk4: krummholz density is ALTITUDE-based (not EDT-distance from a zone
    # that included low rock cliffs). 1.0 below feather_lo (regular taiga, no
    # thinning), then near_density_mult AT feather_lo fading linearly to
    # far_density_mult at density_top_y. So the band is "much sparser" right at
    # the treeline (feather_lo) and fades to nothing at the top.
    _kr_dens = None
    _kr_dens_top = float(_kr_cfg.get("density_top_y", 700.0))
    if _kr_on:
        _sy_f = surface_y.astype(np.float32)
        _alt_t = np.clip((_sy_f - _kr_feather_lo)
                         / max(1.0, _kr_dens_top - _kr_feather_lo), 0.0, 1.0)
        _kr_dens = (_kr_near + (_kr_far - _kr_near) * _alt_t).astype(np.float32)
        _kr_dens[_sy_f < _kr_feather_lo] = 1.0
    _kr_pool = []
    if _kr_on and _kr_whitelist:
        _kr_seen = set()
        for _blist in index.values():
            for _e in _blist:
                if getattr(_e, "schem_type", "tree") != "tree":
                    continue
                if _e.path in _kr_seen:
                    continue
                if any(_w in _e.path for _w in _kr_whitelist):
                    _kr_seen.add(_e.path)
                    # HEIGHT GATE: drop mislabeled tall variants (size codes lie).
                    _kr_h = _krummholz_tree_height(_e.path)
                    if _kr_h is not None and _kr_h > _kr_max_h:
                        continue
                    _kr_pool.append(_e)

    # S89-walk4 verify: env-gated krummholz placement counter by altitude band.
    import os as _os_kr
    _KR_DBG = bool(_os_kr.environ.get("KR_DEBUG"))
    _kr_dbg_bands = [0, 0, 0, 0, 0, 0]          # <450,450-500,500-550,550-575,575-650,>=650
    _kr_dbg_reg = [0, 0, 0, 0, 0, 0]            # regular trees placed, same bands

    def _kr_band(y):
        if y < 450: return 0
        if y < 500: return 1
        if y < 550: return 2
        if y < 575: return 3
        if y < 650: return 4
        return 5

    for pass_type in ("tree", "bush"):
        # Bushes get their own exclusion grid (smaller radius, independent of trees)
        if pass_type == "bush":
            bush_exclusion = _ExclusionGrid(H, W)

        for idx in order:
            row = int(rows_arr[idx])
            col = int(cols_arr[idx])

            orig_biome_str = str(biome_grid[row, col])
            biome_str = orig_biome_str
            # S59: ecotone seam dither — swap to neighbour biome's entries list
            # at rolled pixels so species mix across biome boundaries.
            # S63: guarded by ECOTONE_DENY_PAIRS — don't swap across aesthetic
            # conflicts (e.g. conifers into tropical jungle).
            # S71-3: FROZEN_FLATS opted OUT of ecotone schematic swap — user
            # wants ONLY smallest pines on FF, but adjacent COASTAL_HEATH (and
            # other neighbors) can have md/lg trees that leak in via the swap.
            # Same for ARCTIC_TUNDRA (mountain-snowy, very-very-sparse).
            # S87 user (80,50): RIPARIAN_WOODLAND added.  Trees were not
            # surviving near rivers because the ecotone swap routed RIPARIAN
            # pixels into neighbour biomes that reject placement on floodplain
            # / water-adjacent cells.  Opting RIPARIAN out of swap entirely
            # preserves its tree population at the river's edge.
            _NO_SWAP_BIOMES = frozenset({"FROZEN_FLATS", "ARCTIC_TUNDRA", "RIPARIAN_WOODLAND"})
            swap_active = False
            if (_seam_swap_grid is not None and _seam_swap_grid[row, col]
                    and biome_str not in _NO_SWAP_BIOMES):
                _alt = str(_seam_nb[row, col])
                if (_alt and _alt in index
                        and _alt not in _NO_SWAP_BIOMES
                        and frozenset({biome_str, _alt}) not in ECOTONE_DENY_PAIRS):
                    biome_str = _alt
                    swap_active = True
            all_entries = index.get(biome_str)
            if not all_entries:
                continue

            entries = _filter_entries(all_entries, pass_type)
            if not entries:
                continue

            # S89: a snowy/conifer cell that swapped to a broadleaf neighbour keeps
            # ONLY conifer species (no birch/oak/etc in the snow). Uses the ORIGINAL
            # biome so conifer-conifer mixing still works.
            if (swap_active and pass_type == "tree"
                    and orig_biome_str in _CONIFER_SNOWY_BIOMES):
                entries = [e for e in entries
                           if any(s in e.species for s in _CONIFER_SPECIES_SUBSTR)]
                if not entries:
                    continue

            # S89 krummholz: feathered small-tree restriction. Rock proximity =
            # always small; altitude ramps the probability over [lo,hi] so the
            # band fades in instead of a hard line.
            _kr_hit = False
            if _kr_on and pass_type == "tree":
                _kr_p = 0.0
                _y = float(surface_y[row, col])
                # S89-walk4: ALTITUDE is the gate. Below feather_lo -> regular
                # taiga (NO krummholz even hugging rock/snow -> fixes "krummholz
                # way too low"). At/above feather_lo, rock/snow proximity forces
                # the tiny pool; otherwise P ramps 0->1 over [lo,hi] (quick).
                if _y >= _kr_feather_lo:
                    if ((_kr_rock_near is not None and bool(_kr_rock_near[row, col]))
                            or (_kr_snow_near is not None
                                and bool(_kr_snow_near[row, col]))
                            or _y >= _kr_feather_hi):
                        _kr_p = 1.0
                    else:
                        _kr_p = (_y - _kr_feather_lo) / max(
                            1.0, _kr_feather_hi - _kr_feather_lo)
                if _kr_p > 0.0 and rng.random() < _kr_p:
                    _kr_hit = True               # thinned in the density step below
                    if _kr_pool:
                        entries = _kr_pool          # force the tiny-pine pool
                    else:
                        _kr_sm = [e for e in entries if e.size == "sm"]
                        if _kr_sm:
                            entries = _kr_sm

            # S70 Item M: palm distance gate for LUSH_RAINFOREST_COAST.
            # S71: extended to RAINFOREST_COAST per user walk — palms only
            # fire within 32 blocks of water edge in both coastal-tropic
            # biomes.  Far from water, exclude palm species and fall through
            # to other tropics.
            # S86 Item 1H: ALSO filter palms when ORIGINAL biome (pre-swap)
            # is not a palm-OK coastal biome.  Ecotone swap into LUSH from
            # FRESHWATER_FEN was firing palms inland.  Original biome guards
            # the swap from importing palms into non-coastal pixels.
            _PALM_OK_BIOMES = ("LUSH_RAINFOREST_COAST", "RAINFOREST_COAST")
            if pass_type == "tree":
                _palm_blocked_by_dist = (
                    biome_str in _PALM_OK_BIOMES
                    and dist_to_water_blocks[row, col] >= 32.0
                )
                _palm_blocked_by_origin = (
                    swap_active and orig_biome_str not in _PALM_OK_BIOMES
                )
                if _palm_blocked_by_dist or _palm_blocked_by_origin:
                    entries = [e for e in entries
                               if e.species not in ("rfpalm", "mpalm", "cpalm")]
                    if not entries:
                        continue

            # Base density × noise.
            # S86 Item 1I: at swap pixels, blend source + neighbor densities so
            # the transition reads as a gradient instead of a density spike
            # caused by neighbor's higher BASE_DENSITY suddenly taking over.
            if swap_active:
                # S89 Option C: at an ecotone-swapped pixel, hold the DENSER
                # side's density (max) instead of averaging the two -- so a
                # dense forest keeps its density into the transition and only
                # the sparse side gains trees, never the reverse. "mean" keeps
                # the old averaging behaviour.
                _d_orig = BASE_DENSITY.get(orig_biome_str, 0.05)
                _d_nbr = BASE_DENSITY.get(biome_str, 0.05)
                _blend = cfg.get("eco_placement", {}).get(
                    "ecotone_density_blend", "max")
                base_d = (max(_d_orig, _d_nbr) if _blend == "max"
                          else 0.5 * (_d_orig + _d_nbr))
            else:
                base_d = BASE_DENSITY.get(biome_str, 0.05)
            final_d = base_d * float(density_mult[row, col])

            # Eco density modulation
            if eco_density_tile is not None:
                final_d *= float(eco_density_tile[row, col])

            # Bush density scaling
            if pass_type == "bush":
                _abs_bush = BUSH_DENSITY_ABS.get(biome_str)
                if _abs_bush is not None:
                    # S89 walk: ABSOLUTE bush target. final_d currently carries
                    # base_d × noise × eco; rescale base_d → target to keep that
                    # organic modulation while hitting the requested rate.
                    final_d *= (_abs_bush / max(base_d, 1e-9))
                    if biome_str in SPARSE_BUSH_BIOMES:
                        final_d *= 0.5
                    # KARST grove clustering still applies (scale-60 simplex).
                    if biome_str == "KARST_BARRENS":
                        final_d *= float(karst_density_mult[row, col])
                else:
                    final_d *= 0.4
                    if biome_str in SPARSE_BUSH_BIOMES:
                        final_d *= 0.5
                    # S86 Item 3C: per-biome bush multiplier on top of base.
                    final_d *= BUSH_DENSITY_MULT.get(biome_str, 1.0)
                    # S71-2: CONTINENTAL_STEPPE bush boost — user wants ~half the
                    # trees replaced with bushes.  4x on the halved BASE_DENSITY.
                    if biome_str == "CONTINENTAL_STEPPE":
                        final_d *= 4.0
                # S89 walk3: bushes thin in the krummholz zone JUST LIKE trees
                # (user) -- _kr_dens is 1.0 outside the zone and fades to FF
                # sparseness deep inside, so this is a no-op elsewhere.
                if _kr_dens is not None:
                    final_d *= float(_kr_dens[row, col])
            else:  # tree pass
                # S71-3: ARCTIC_TUNDRA tree thinning — "VERY VERY sparse"
                # smallest pines.  AT BASE 0.005 × 0.05 = ~0.00025.
                if biome_str == "ARCTIC_TUNDRA":
                    final_d *= 0.05
                # S71-3 swap: FROZEN_FLATS smallest-pine thinning — user wants
                # "very very very sparse".  FF BASE 0.04 × 0.03 = ~0.0012 per
                # pixel ≈ ~6-12 small spruces per tile.
                if biome_str == "FROZEN_FLATS":
                    final_d *= 0.03
                # S89 walk fix: krummholz is a SPARSE stunted band, not a fill.
                # The force-small logic above doesn't reduce COUNT, so on a
                # high-density biome (SBT BASE=1.0) every candidate near rock/snow
                # became a tiny pine -> dense carpet. Thin krummholz cells hard so
                # they read as scattered wind-pruned trees at the treeline.
                if _kr_hit:
                    final_d *= (float(_kr_dens[row, col]) if _kr_dens is not None
                                else _kr_density_mult)

            if rng.random() >= final_d:
                continue

            # Habitat-weighted species selection
            if species_scores:
                weights = []
                for e in entries:
                    sp_score = species_scores.get(e.species)
                    if sp_score is not None:
                        h_score = max(float(sp_score[row, col]), score_floor)
                    else:
                        h_score = 1.0
                    weights.append(e.weight * h_score)
            else:
                weights = [e.weight for e in entries]

            if max(weights) <= 0:
                continue

            entry = rng.choices(entries, weights=weights, k=1)[0]

            # No-repeat rule: reject if same schematic was placed nearby.
            # S71-3: tightened from 30→60 history + 0.01→0.0001 suppress weight
            # + species-level dedupe in addition to path.  User walk feedback
            # on BIRCH_FOREST: identical trees clustering in adjacency,
            # gridlike placement.  Larger history catches more recent neighbors;
            # near-zero suppress-weight makes re-rolls almost always pick a
            # different schematic; species-level dedupe catches different
            # variants of the same species (a/b/c sm) being treated as
            # "different" by the path check.
            entry_path = entry.path
            entry_species = entry.species
            radius = CANOPY_RADIUS.get(entry.size, 4)
            reject_radius = radius * 2
            is_dupe = False
            for prev in placements[max(0, len(placements)-60):]:
                if prev.schem_path == entry_path or (
                    entry_species != "generic"
                    and getattr(prev, "species", "generic") == entry_species
                    and abs(prev.world_z - (py_off + row)) <= reject_radius
                    and abs(prev.world_x - (px_off + col)) <= reject_radius
                ):
                    dr = abs(prev.world_z - (py_off + row))
                    dc = abs(prev.world_x - (px_off + col))
                    if dr <= reject_radius and dc <= reject_radius:
                        is_dupe = True
                        break
            if is_dupe:
                # Re-roll with strong suppression on the dupe schematic AND
                # other entries of the same species (stronger anti-clustering).
                alt_weights = []
                for e, w in zip(entries, weights):
                    if e.path == entry_path:
                        alt_weights.append(w * 0.0001)
                    elif (entry_species != "generic" and e.species == entry_species):
                        alt_weights.append(w * 0.10)
                    else:
                        alt_weights.append(w)
                if max(alt_weights) > 0:
                    entry = rng.choices(entries, weights=alt_weights, k=1)[0]

            # Canopy exclusion check
            # S87 walk #1 #2 (26,10): bush placements must respect TREE
            # exclusion grid in addition to bush exclusion, so bushes don't
            # land inside tree footprints and overwrite tree blocks.
            # S89 Option A: per-biome canopy radius (density ceiling lever).
            # Same `radius` feeds both is_clear AND the mark below.
            radius = _biome_canopy_radius(entry.size, biome_str, cfg)
            if pass_type == "tree":
                if not exclusion.is_clear(row, col, radius):
                    continue
            else:
                bush_r = max(1, radius // 2)
                # S98 island-only: SEMI_ARID packs bushes shoulder-to-shoulder ("can't
                # see the ground") -> drop the exclusion floor below 1 (adjacent OK).
                if _SARID_ISLAND_DENSE and biome_str == "SEMI_ARID_SHRUBLAND":
                    bush_r = radius // 2
                if not bush_exclusion.is_clear(row, col, bush_r):
                    continue
                # Cross-check: don't place a bush where a tree was placed.
                if not exclusion.is_clear(row, col, bush_r):
                    continue

            # Position jitter: ±3 blocks to break grid regularity (S71-3 was ±2)
            jitter_x = rng.randint(-3, 3)
            jitter_z = rng.randint(-3, 3)
            jittered_col = max(0, min(W - 1, col + jitter_x))
            jittered_row = max(0, min(H - 1, row + jitter_z))

            # S91 regression #2: reject placements whose schematic would cross
            # the tile boundary. stamp_schematic anchors at the (0,0) corner
            # and "clips to tile bounds silently" — every anchor within the
            # schematic's XZ extent of the right/bottom edge rendered as a
            # vertically-sliced half-tree lining the tile border (~tens per
            # forested seam, world-wide). The neighbour tile is generated
            # independently and cannot render the missing half, so rejection
            # is the only seam-safe option. Extent is the ACTUAL max(X,Z) of
            # the schematic (median ~10, up to ~21 — the footprint proxy
            # 2*center_off+1 under-covers badly), loaded lazily and cached;
            # only candidates within _EDGE_GUARD of the far edges pay the
            # load. max(X,Z) also covers all 4 rotations.
            # S96 NOTE: the S92 edge-guard is INTENTIONALLY left always-on here
            # (NOT gated by tree_seam).  Gating it changes the interior rng draw
            # sequence for every subsequent candidate (the S91 "forests RE-ROLL"
            # footgun) — measured as a ~5pt shift in DEEP-interior canopy, which
            # violates the mainland-density invariant + the 48_48/51_53 baselines.
            # Keeping it intact makes flag-OFF byte-identical to the pre-S96
            # baseline AND keeps flag-ON's interior stream identical to flag-OFF;
            # the world-coord rim pass (after this loop) is then a pure ADDITION
            # whose seam trees coexist with the S92 substitutes via the shared
            # exclusion grid (rim trees overlapping a guard tree are excluded).
            _EDGE_GUARD = 24
            if (jittered_col >= W - _EDGE_GUARD) or (jittered_row >= H - _EDGE_GUARD):
                def _edge_extent(_e):
                    _x = _schem_extent_cache.get(_e.path)
                    if _x is None:
                        try:
                            from core.schematic_loader import load_schem as _ld_ext
                            _sd_ext = _ld_ext(_e.path)
                            _x = int(max(_sd_ext.blocks.shape[1],
                                         _sd_ext.blocks.shape[2]))
                        except Exception:
                            _x = 2 * {"sm": 2, "md": 3, "lg": 4}.get(_e.size, 3) + 5
                        _schem_extent_cache[_e.path] = _x
                    return _x

                _fit_lim = min(W - jittered_col, H - jittered_row)
                if _edge_extent(entry) > _fit_lim:
                    # S92: SUBSTITUTE a same-list schematic that actually fits
                    # instead of rejecting. S91's plain rejection left a bare
                    # trunk-AND-canopy lane ~10px wide tracing every tile
                    # border (user screenshot: dead-straight gap through
                    # canopy at the 50|51 seam). Substitution keeps placements
                    # marching right up to the border with progressively
                    # smaller schematics; interior canopies then overhang the
                    # last few px. Weighted among the fitting entries.
                    _fit_pairs = [(e, w) for e, w in zip(entries, weights)
                                  if w > 0 and _edge_extent(e) <= _fit_lim]
                    if not _fit_pairs:
                        continue
                    entry = rng.choices([p[0] for p in _fit_pairs],
                                        weights=[p[1] for p in _fit_pairs],
                                        k=1)[0]
                    # keep downstream sizing consistent with the substitute
                    radius = _biome_canopy_radius(entry.size, biome_str, cfg)

            # Compute placement Y at jittered position
            sy         = int(surface_y[jittered_row, jittered_col])

            # Footprint range for per-size slope reject. _center_off is the
            # approximate trunk offset from the (0,0) corner (half of the
            # schematic's XZ extent for the size tier). Kept here as a
            # footprint sampling span; sy anchoring stays at sample pixel.
            _SIZE_CENTER_OFF = {"sm": 2, "md": 3, "lg": 4}
            _center_off = _SIZE_CENTER_OFF.get(entry.size, 3)

            # S60 hard-reject: per-size footprint sy-range threshold. Larger
            # schematics get tighter slope tolerance because their wider
            # footprint is more likely to straddle terrain variation.
            _fp_r0 = max(0, jittered_row - 1)
            _fp_r1 = min(H, jittered_row + 2 * _center_off + 1)
            _fp_c0 = max(0, jittered_col - 1)
            _fp_c1 = min(W, jittered_col + 2 * _center_off + 1)
            _fp = surface_y[_fp_r0:_fp_r1, _fp_c0:_fp_c1]
            _MAX_FP_RANGE_BY_SIZE = {"sm": 4, "md": 3, "lg": 2}
            _max_range = _MAX_FP_RANGE_BY_SIZE.get(entry.size, 3)
            if int(_fp.max() - _fp.min()) > _max_range:
                continue  # terrain too steep under footprint — skip placement

            base_y     = sy - entry.anchor_y - entry.inset_depth
            extra      = _compute_extra_inset(
                entry.size, entry.method, base_y,
                entry.lowest_leaf_y, sy, rng,
            )
            place_y    = base_y - extra

            # S86 Item 1D: adjacency-aware rotation selection.
            # Random rotation (0=0deg, 1=90, 2=180, 3=270).  To prevent the
            # "two identical trees next to each other facing the same way"
            # artifact (user feedback on 20,36 / 33,49 / 18,62), track rotations
            # used in nearby 4x4-cell neighborhoods and prefer a rotation that
            # differs from neighbors.  Applies to *any* adjacent tree
            # schematics (not just same species — user clarified).
            # S89 walk2: widen the neighborhood from +/-1 to +/-2 cells (4-block
            # cells -> ~20-block span) so adjacent trees are far more likely to
            # get distinct rotations -- the +/-1 window saturated in dense forest
            # and let close pairs share a rotation ("cloned trees").
            _cell_r = jittered_row // 4
            _cell_c = jittered_col // 4
            _NB = (-2, -1, 0, 1, 2)
            _used = set()
            for _dr in _NB:
                for _dc in _NB:
                    _used.update(_rotation_grid.get((_cell_r + _dr, _cell_c + _dc), ()))
            _candidates = [r for r in (0, 1, 2, 3) if r not in _used]
            if not _candidates:
                # Saturated cell: pick the least-used rotation among neighbors
                _counts = {r: 0 for r in (0, 1, 2, 3)}
                for _dr in _NB:
                    for _dc in _NB:
                        for _r in _rotation_grid.get((_cell_r + _dr, _cell_c + _dc), ()):
                            _counts[_r] += 1
                _min = min(_counts.values())
                _candidates = [r for r, c in _counts.items() if c == _min]
            rotation = rng.choice(_candidates)
            _rotation_grid.setdefault((_cell_r, _cell_c), []).append(rotation)

            world_x = px_off + jittered_col
            world_z = py_off + jittered_row

            placements.append(PlacementRecord(
                schem_path  = entry.path,
                world_x     = world_x,
                world_z     = world_z,
                place_y     = place_y,
                anchor_y    = entry.anchor_y,
                inset_depth = entry.inset_depth,
                extra_inset = extra,
                size        = entry.size,
                schem_type  = entry.schem_type,
                biome       = biome_str,
                rotation    = rotation,
                species     = entry.species,
            ))

            # Mark exclusion zone
            if pass_type == "tree":
                exclusion.mark(row, col, radius)
                if _KR_DBG:
                    _b = _kr_band(int(surface_y[row, col]))
                    if _kr_hit:
                        _kr_dbg_bands[_b] += 1
                    else:
                        _kr_dbg_reg[_b] += 1
            else:
                _bmark = (radius // 2 if (_SARID_ISLAND_DENSE and biome_str == "SEMI_ARID_SHRUBLAND")
                          else max(1, radius // 2))
                bush_exclusion.mark(row, col, _bmark)

    # ====================================================================
    # S96 SEAM RIM PASS — world-coord DETERMINISTIC tree+bush reconstruction
    # --------------------------------------------------------------------
    # Runs AFTER the interior loop (interior is canonical at the PAD edge).
    # For every cell within rim_halo_px of a tile edge — INCLUDING a halo of
    # negative / >=W,H local coords owned by the neighbour — both adjacent
    # tiles compute IDENTICAL world_x/world_z and feed them through _wc_u01s,
    # so they accept/reject/jitter/species/rotate the SAME seam trees.  Each
    # tile appends the record; chunk_writer's anchor-cull (STEP A) drops the
    # copy whose anchor is OOB for that tile, and the OTHER tile renders it
    # with an in-bounds anchor + clips the overhang.  Net: the shared seam
    # tree renders once, clipped at the seam, from both sides — no trench.
    # Density/species use ONLY world-coord-shared inputs (world-coord density
    # noise + global index weights) so the two tiles agree bit-for-bit.  The rim
    # RENDERS a tree only when its jittered ANCHOR lands IN-BOUNDS on a LOW edge
    # (col<RIM or row<RIM): that's the only path chunk_writer stamps (min-corner
    # anchoring + canopy in +X/+Z means the low edge is what the interior loop
    # under-covers; high edges are interior-owned).  OOB-anchor candidates are
    # DROPPED (not appended) — chunk_writer culls them anyway, and emitting them
    # would risk an asymmetric "floating half-canopy" (S96 BUG 4).  Each rendered
    # rim tree is gated by the SAME land_mask the interior used (water buffer +
    # gap full-suppress + clearing + snow surface — S96 BUG 3), seats on the
    # PRE-CARVE padded surface so its Y matches the neighbour's view at a
    # river/relief seam (S96 BUG 2), and draws extra_inset from a world-anchor
    # hash so both clipped halves seat identically (S96 BUG 1).
    if _seam_on and surface_y_padded is not None:
        _RIM = int(_seam_cfg.get("rim_halo_px", 24))
        # S96 rim-density parity (island canopy-seam trench fix).  The interior
        # loop places greedily from a shuffled candidate list (dense); the rim
        # rolls a single per-cell coin (sparse), so the low edge filled to only
        # ~half interior density -> a residual ~10-15px canopy trench at every
        # tile seam (worst on dense LUSH jungle islands).  Boost the rim's
        # per-cell accept probability toward interior parity; the shared
        # exclusion grid still prevents over-stacking.  Rim-path ONLY (the
        # interior accept test is untouched), so flag-OFF + mainland-byte
        # behaviour are unchanged.
        _RIM_DEN_BOOST = float(_seam_cfg.get("rim_density_boost", 1.0))
        # Pad offsets: biome_grid_padded uses its OWN half-pad (S93c ecotone
        # pad, typically 48); surface_y_padded uses seam_pad_px (typically 96).
        _bg_pad = None
        if (biome_grid_padded is not None and getattr(biome_grid_padded, "ndim", 0) == 2
                and biome_grid_padded.shape[0] > H and biome_grid_padded.shape[1] > W
                and (biome_grid_padded.shape[0] - H) == (biome_grid_padded.shape[1] - W)):
            _bg_pad = (biome_grid_padded.shape[0] - H) // 2
        _sy_pad = int(seam_pad_px)
        _sy_ok = (getattr(surface_y_padded, "ndim", 0) == 2
                  and surface_y_padded.shape[0] >= H + 2 * _sy_pad
                  and surface_y_padded.shape[1] >= W + 2 * _sy_pad)
        # S96 BUG 2: PRE-CARVE padded surface for symmetric place_y.  Falls back
        # to the (post-carve-inner) surface_y_padded if not supplied or mis-sized.
        _pc_pad = surface_y_precarve_padded
        if (_pc_pad is None or getattr(_pc_pad, "ndim", 0) != 2
                or _pc_pad.shape != surface_y_padded.shape):
            _pc_pad = surface_y_padded
        # Only run if BOTH halos cover the rim band (else fall through silently).
        if _bg_pad is not None and _bg_pad >= _RIM and _sy_ok and _sy_pad >= _RIM:
            # Determinism diagnostic: when SEAM_RIM_DUMP is set, record EVERY rim
            # emission's (world_x, world_z, species, type) — regardless of cull —
            # so two adjacent tiles can be cross-checked for IDENTICAL seam trees.
            import os as _os_rim
            _rim_dbg = _os_rim.environ.get("SEAM_RIM_DUMP")
            _rim_dbg_rows: list = [] if _rim_dbg else None
            # Tree biomes that exist in the index with TREE entries (precompute
            # the per-biome tree/bush entry lists + base weights ONCE).
            _rim_entries_cache: dict[tuple[str, str], tuple] = {}

            def _rim_entries(_biome, _ptype):
                _k = (_biome, _ptype)
                _c = _rim_entries_cache.get(_k)
                if _c is not None:
                    return _c
                _all = index.get(_biome)
                if not _all:
                    _rim_entries_cache[_k] = ((), ())
                    return ((), ())
                _es = tuple(_filter_entries(_all, _ptype))
                _ws = tuple(float(max(e.weight, 0.0)) for e in _es)
                if not _es or sum(_ws) <= 0.0:
                    _rim_entries_cache[_k] = ((), ())
                    return ((), ())
                _rim_entries_cache[_k] = (_es, _ws)
                return (_es, _ws)

            def _rim_pick(_es, _ws, u):
                # Deterministic cumulative weighted pick (mirrors rng.choices).
                _tot = 0.0
                for _w in _ws:
                    _tot += _w
                _r = u * _tot
                _acc = 0.0
                for _e, _w in zip(_es, _ws):
                    _acc += _w
                    if _r < _acc:
                        return _e
                return _es[-1]

            # Enumerate ONLY the TOP and LEFT bands — the rim fills a tile's LOW
            # edges (rows/cols near 0); the HIGH (right/bottom) edges are
            # interior-owned (canopy overhangs them).  A high-edge cell can never
            # jitter (±3) below _RIM, so iterating it is pure waste.  Each band
            # spans local coords in [-_RIM, _RIM) on its edge axis and the FULL
            # tile + halo on the other, so a neighbour-owned anchor at -_RIM that
            # jitters in-bounds is still considered.  Sets dedup the corner.
            _rim_cells: set[tuple[int, int]] = set()
            for _r in range(-_RIM, _RIM):                 # top (low-Z) band
                for _c in range(-_RIM, W + _RIM):
                    _rim_cells.add((_r, _c))
            for _c in range(-_RIM, _RIM):                 # left (low-X) band
                for _r in range(-_RIM, H + _RIM):
                    _rim_cells.add((_r, _c))

            _DEN_FLOOR_R = den_floor
            _DEN_SCALE_R = den_scale
            _DEN_OCT_R = den_oct
            _SNOW_BLOCKS_R = _SNOW_SURFACE_BLOCKS

            for (_lr, _lc) in _rim_cells:
                world_x = px_off + _lc
                world_z = py_off + _lr
                # Biome from the ecotone halo (covers the rim overhang).
                _bi = int(_lr + _bg_pad)
                _bj = int(_lc + _bg_pad)
                _biome_r = str(biome_grid_padded[_bi, _bj])
                # Surface from the seam halo.
                _si = int(_lr + _sy_pad)
                _sj = int(_lc + _sy_pad)
                _sy_r = int(surface_y_padded[_si, _sj])
                if _sy_r < 63:
                    continue  # not land

                # World-coord-shared density noise (identical from both tiles).
                _nz = _fbm(den_gen, world_x / _DEN_SCALE_R, world_z / _DEN_SCALE_R, _DEN_OCT_R)
                _dmult = _DEN_FLOOR_R + (1.0 - _DEN_FLOOR_R) * _nz

                for _ptype, _excl, _salt_a, _salt_sp, _salt_jx, _salt_jz, _salt_rot in (
                    ("tree", exclusion, 0xA1, 0xE5, 0xB2, 0xB3, 0xF0),
                    ("bush", bush_exclusion, 0xA2, 0xE6, 0xB4, 0xB5, 0xF1),
                ):
                    _es, _ws = _rim_entries(_biome_r, _ptype)
                    if not _es:
                        continue
                    # final_d uses ONLY world-coord-SHARED inputs so both
                    # adjacent tiles compute the SAME threshold for a shared seam
                    # cell: BASE_DENSITY[biome] (biome-keyed const) × world-coord
                    # density noise × biome-keyed scalar multipliers.  The
                    # tile-LOCAL eco_density_tile / karst_density_mult are
                    # DELIBERATELY EXCLUDED — edge-replicating them gave each tile
                    # a different value at the halo (plan risk #2), which broke
                    # the accept/reject coin symmetry (measured: rim emissions
                    # agreed only ~9% across the seam).  On flat forest eco≈1 so
                    # the density is unchanged; on a steep/treeline seam the slope
                    # thinning is dropped here, slightly over-placing at the very
                    # edge — bounded by the canopy-exclusion spacing, and the rim
                    # only fills the LOW edge (≤RIM px).
                    base_d = BASE_DENSITY.get(_biome_r, 0.05)
                    final_d = base_d * _dmult
                    if _ptype == "bush":
                        _abs_bush = BUSH_DENSITY_ABS.get(_biome_r)
                        if _abs_bush is not None:
                            final_d *= (_abs_bush / max(base_d, 1e-9))
                            if _biome_r in SPARSE_BUSH_BIOMES:
                                final_d *= 0.5
                        else:
                            final_d *= 0.4
                            if _biome_r in SPARSE_BUSH_BIOMES:
                                final_d *= 0.5
                            final_d *= BUSH_DENSITY_MULT.get(_biome_r, 1.0)
                            if _biome_r == "CONTINENTAL_STEPPE":
                                final_d *= 4.0
                    else:
                        if _biome_r == "ARCTIC_TUNDRA":
                            final_d *= 0.05
                        if _biome_r == "FROZEN_FLATS":
                            final_d *= 0.03

                    # Rim-density parity boost (both tree + bush rim paths share
                    # this accept test).  Lifts the sparse per-cell rim coin to
                    # ~interior density so the low edge fills enough that canopy
                    # reaches the seam; the shared exclusion grid caps stacking.
                    # Clamp <=1.0 so a high biome density * boost can't overflow.
                    final_d = min(final_d * _RIM_DEN_BOOST, 1.0)

                    if _wc_u01s(world_x, world_z, _salt_a) >= final_d:
                        continue

                    # Deterministic species pick (world-coord-shared weights).
                    entry = _rim_pick(_es, _ws, _wc_u01s(world_x, world_z, _salt_sp))
                    radius = _biome_canopy_radius(entry.size, _biome_r, cfg)

                    # Deterministic jitter — true coords (anchor may be OOB).
                    jx = int(_wc_u01s(world_x, world_z, _salt_jx) * 7) - 3
                    jz = int(_wc_u01s(world_x, world_z, _salt_jz) * 7) - 3
                    j_lr = _lr + jz
                    j_lc = _lc + jx

                    # S96: the rim RENDERS only IN-BOUNDS, LOW-edge anchors.  An
                    # OOB jittered anchor (j_lc<0 / >=W / j_lr<0 / >=H) is culled
                    # by chunk_writer, so we DROP it here (no asymmetric float —
                    # BUG 4).  A HIGH-edge in-bounds anchor (col>=RIM and row>=RIM)
                    # is interior-owned, skip it too.  What survives is exactly the
                    # set THIS tile stamps: its low edge.
                    _low_edge_anchor = (0 <= j_lr < H) and (0 <= j_lc < W) and (
                        j_lc < _RIM or j_lr < _RIM)
                    if not _low_edge_anchor:
                        continue

                    # BUG 3: gate by the SAME land_mask the interior used — water
                    # buffer + gap full-suppress (rock/snow/floodplain/meadow/
                    # windthrow/sand_dune/beach, honouring all interior exceptions)
                    # + clearing-interior + snow-surface.  j_lr,j_lc are in-bounds
                    # here so the inner mask is directly indexable.
                    if not bool(land_mask[j_lr, j_lc]):
                        continue

                    # Share the interior exclusion grids (this tile owns them;
                    # interior ran first → canonical at the boundary).
                    if _ptype == "tree":
                        if not _excl.is_clear(j_lr, j_lc, radius):
                            continue
                    else:
                        _br = max(1, radius // 2)
                        if not _excl.is_clear(j_lr, j_lc, _br):
                            continue
                        if not exclusion.is_clear(j_lr, j_lc, _br):
                            continue

                    # Snow-surface skip (land_mask already folds snow_surface_mask
                    # in, but that mask exempts AT/FF/SBT — keep the hard block
                    # check too so no tree seats on raw snow/ice at the seam).
                    if (_ptype == "tree" and surface_blocks is not None
                            and _SNOW_BLOCKS_R
                            and str(surface_blocks[j_lr, j_lc]) in _SNOW_BLOCKS_R):
                        continue

                    # BUG 2: place_y surface.  Because the rim now stamps ONLY
                    # in-bounds LOW-edge anchors (each seam tree rendered by its
                    # ONE owning tile, never duplicated), the accurate POST-CARVE
                    # inner surface_y is directly available and is what the
                    # interior trees on this tile use — so a rim tree seats
                    # exactly like its interior neighbour (no Y step where the
                    # rim meets the interior fill).  We compare it against the
                    # PRE-CARVE padded surface and take the LOWER (deeper) of the
                    # two so a tree never floats over a carved channel/relief dip
                    # that the post-carve value already reflects; this also keeps
                    # the two sides of the seam (interior high-edge vs rim
                    # low-edge) reading the same seam-continuous carved terrain.
                    sy_j = int(surface_y[j_lr, j_lc])
                    _jsi = int(j_lr + _sy_pad)
                    _jsj = int(j_lc + _sy_pad)
                    if (0 <= _jsi < _pc_pad.shape[0]
                            and 0 <= _jsj < _pc_pad.shape[1]):
                        sy_j = min(sy_j, int(_pc_pad[_jsi, _jsj]))
                    base_y = sy_j - entry.anchor_y - entry.inset_depth
                    # BUG 1: extra_inset from a world-ANCHOR hash (not tile rng) so
                    # both adjacent tiles compute the SAME inset for a shared tree.
                    _u_extra = _wc_u01s(px_off + j_lc, py_off + j_lr, 0xE7)
                    extra = _compute_extra_inset_u(
                        entry.size, entry.method, base_y,
                        entry.lowest_leaf_y, sy_j, _u_extra)
                    place_y = base_y - extra
                    rotation = int(_wc_u01s(world_x, world_z, _salt_rot) * 4) & 3

                    if _rim_dbg_rows is not None:
                        _rim_dbg_rows.append(
                            (px_off + j_lc, py_off + j_lr, _ptype,
                             entry.species, entry.path, 1))

                    placements.append(PlacementRecord(
                        schem_path  = entry.path,
                        world_x     = px_off + j_lc,
                        world_z     = py_off + j_lr,
                        place_y     = place_y,
                        anchor_y    = entry.anchor_y,
                        inset_depth = entry.inset_depth,
                        extra_inset = extra,
                        size        = entry.size,
                        schem_type  = entry.schem_type,
                        biome       = _biome_r,
                        rotation    = rotation,
                        species     = entry.species,
                    ))
                    # All kept rim cells are in-bounds → mark the shared grid.
                    if _ptype == "tree":
                        _excl.mark(j_lr, j_lc, radius)
                    else:
                        _excl.mark(j_lr, j_lc, max(1, radius // 2))

            if _rim_dbg_rows is not None:
                try:
                    import numpy as _np_rd
                    _np_rd.save(
                        f"{_rim_dbg}/rim_{tile_x}_{tile_y}.npy",
                        _np_rd.array(_rim_dbg_rows, dtype=object),
                        allow_pickle=True)
                    print(f"[SEAM_RIM_DUMP] tile=({tile_x},{tile_y}) "
                          f"rim emissions: {len(_rim_dbg_rows)}", flush=True)
                except Exception as _rd_e:
                    print(f"[SEAM_RIM_DUMP] WARN: {_rd_e}", flush=True)

    if _KR_DBG:
        _lbls = ["<450", "450-500", "500-550", "550-575", "575-650", ">=650"]
        print(f"[KR_DEBUG] tile=({tile_x},{tile_y}) krummholz vs regular trees placed by band:")
        for _i, _l in enumerate(_lbls):
            print(f"[KR_DEBUG]   {_l:>8}: krummholz={_kr_dbg_bands[_i]:>5}  regular={_kr_dbg_reg[_i]:>5}")

    return placements


# ---------------------------------------------------------------------------
# SMOKE TEST (stdlib only — no amulet, no rasterio, no opensimplex)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import tempfile

    print("schematic_placement.py — smoke test")

    # --- Stub opensimplex ---------------------------------------------------
    class _FakeGen:
        def noise2(self, x, y): return 0.0

    noise_fields = {"decoration_density": _FakeGen()}

    # --- Build a minimal in-memory index -----------------------------------
    H, W = 64, 64
    tile_x, tile_y = 2, 3

    fake_index: dict[str, list[_SchematicEntry]] = {
        "MIXED_FOREST": [
            _SchematicEntry(
                path="schematics/mixed/oak_md_01.schem",
                biome="MIXED_FOREST", size="md", schem_type="tree",
                anchor_y=3, inset_depth=2, lowest_leaf_y=7,
                method="trunk_at_bottom", weight=2, anchor_review=False,
            ),
            _SchematicEntry(
                path="schematics/mixed/bush_generic_sm_01.schem",
                biome="MIXED_FOREST", size="sm", schem_type="bush",
                anchor_y=0, inset_depth=0, lowest_leaf_y=2,
                method="no_trunk_lowest_solid", weight=3, anchor_review=False,
            ),
        ],
        "ARCTIC_TUNDRA": [
            _SchematicEntry(
                path="schematics/tundra/dead_shrub_sm_01.schem",
                biome="ARCTIC_TUNDRA", size="sm", schem_type="bush",
                anchor_y=0, inset_depth=0, lowest_leaf_y=1,
                method="no_trunk_lowest_solid", weight=3, anchor_review=False,
            ),
        ],
        "SAND_DUNE_DESERT": [
            _SchematicEntry(
                path="schematics/desert/dead_bush_sm_01.schem",
                biome="SAND_DUNE_DESERT", size="sm", schem_type="bush",
                anchor_y=0, inset_depth=0, lowest_leaf_y=1,
                method="no_trunk_lowest_solid", weight=3, anchor_review=False,
            ),
        ],
    }

    np_rng = np.random.default_rng(99)
    surface_y    = np.full((H, W), 80, dtype=np.int16)
    biome_grid   = np.full((H, W), "MIXED_FOREST", dtype=object)
    biome_grid[:H//4, :]        = "ARCTIC_TUNDRA"
    biome_grid[H//2:, W//2:]    = "SAND_DUNE_DESERT"
    river_meta   = np.zeros((H, W), dtype=np.uint8)
    river_meta[30:34, :]        = 2   # fake river band
    moisture_tile = np_rng.random((H, W)).astype(np.float32)

    cfg = {
        "decoration_density_noise": {"scale": 60, "octaves": 3, "floor": 0.15},
    }

    results = place_schematics(
        surface_y, biome_grid, river_meta, moisture_tile,
        noise_fields, cfg, fake_index, tile_x, tile_y,
    )

    # Assertions
    assert isinstance(results, list), "expected list"

    # No placement on river bank pixels
    for p in results:
        local_z = p.world_z - tile_y * H
        local_x = p.world_x - tile_x * W
        assert river_meta[local_z, local_x] == 0, \
            f"placement on river bank pixel at ({local_x}, {local_z})"

    # place_y sanity: should be below surface
    for p in results:
        local_z = p.world_z - tile_y * H
        local_x = p.world_x - tile_x * W
        sy = int(surface_y[local_z, local_x])
        assert p.place_y <= sy, \
            f"place_y {p.place_y} above surface {sy}"

    # No anchor_review entries should have slipped through
    for p in results:
        assert p.schem_path, "empty schem_path"

    biomes_placed = set(p.biome for p in results)
    sizes_placed  = set(p.size  for p in results)
    types_placed  = set(p.schem_type for p in results)

    print(f"  placements total  : {len(results)}")
    print(f"  biomes represented: {sorted(biomes_placed)}")
    print(f"  sizes placed      : {sorted(sizes_placed)}")
    print(f"  types placed      : {sorted(types_placed)}")
    print(f"  extra_inset range : {min(p.extra_inset for p in results) if results else 'n/a'}"
          f"–{max(p.extra_inset for p in results) if results else 'n/a'}")
    print("PASS")
    sys.exit(0)
