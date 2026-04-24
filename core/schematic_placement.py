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
    "ARCTIC_TUNDRA", "FROZEN_FLATS",
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

# Canopy overlap exclusion radii (pixels) per size code
CANOPY_RADIUS: dict[str, int] = {
    "sm": 3,
    "md": 5,
    "lg": 8,
}

# Base placement density per biome (probability a candidate pixel is attempted)
# Tuned conservatively — final density also multiplied by decoration noise
BASE_DENSITY: dict[str, float] = {
    "COASTAL_HEATH":           0.05,
    "TEMPERATE_RAINFOREST":    0.26,
    "BOREAL_TAIGA":            0.22,
    "SNOWY_BOREAL_TAIGA":      0.12,
    "ARCTIC_TUNDRA":           0.015,
    "FROZEN_FLATS":            0.00,
    "TEMPERATE_DECIDUOUS":     0.22,
    "RAINFOREST_COAST":        0.24,
    "RIPARIAN_WOODLAND":       0.18,
    "DRY_OAK_SAVANNA":         0.09,
    "KARST_BARRENS":           0.20,
    "BIRCH_FOREST":            0.20,
    "EASTERN_TEMPERATE_COAST": 0.06,
    "MIXED_FOREST":            0.22,
    "CONTINENTAL_STEPPE":      0.06,
    "DRY_PINE_BARRENS":        0.14,
    "SCRUBBY_HEATHLAND":       0.06,
    "LUSH_RAINFOREST_COAST":   0.26,
    "SAND_DUNE_DESERT":        0.008,
    "DESERT_STEPPE_TRANSITION":0.03,
    "SEMI_ARID_SHRUBLAND":     0.05,
    "DRY_WOODLAND_MAQUIS":     0.10,
    "TIDAL_JUNGLE_FRINGE":     0.15,
    "MANGROVE_COAST":          0.08,
    "FRESHWATER_FEN":          0.12,
}


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

    # Suppress trees in clearing gaps (gap_mask from eco_gradients)
    if eco_grads is not None and hasattr(eco_grads, 'gap_mask'):
        gap = eco_grads.gap_mask
        # Full suppression: meadow, windthrow, floodplain, bare rock
        full_suppress = (gap == 1) | (gap == 2) | (gap == 4) | (gap == 5) | (gap == 7) | (gap == 8)
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
        lo, hi = noise_tile.min(), noise_tile.max()
        if hi - lo > 1e-9:
            noise_tile = (noise_tile - lo) / (hi - lo)
        else:
            noise_tile = np.full((H, W), 0.5, dtype=np.float32)
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
    if eco_grads is not None and hasattr(eco_grads, 'rock_exposure_gradient'):
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
        _default = treelines_cfg.get("_default", {"y_top": 230, "fade_blocks": 30})
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
    _SNOW_SURFACE_BLOCKS = frozenset({
        "snow", "snow_block", "powder_snow", "ice", "packed_ice", "blue_ice",
    })
    snow_surface_mask = None
    if surface_blocks is not None:
        snow_surface_mask = np.zeros((H, W), dtype=bool)
        for _blk in _SNOW_SURFACE_BLOCKS:
            snow_surface_mask |= (surface_blocks == _blk)
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
    # Inner-only (no padding) — cross-tile symmetry is cosmetic carry-forward.
    _seam_has_nb: np.ndarray | None = None
    _seam_nb: np.ndarray | None = None
    _seam_swap_grid: np.ndarray | None = None
    try:
        from core.surface_decorator import _compute_ecotone_swap_fields as _ecotone_fields
        _fields = _ecotone_fields(biome_grid, cfg, gap_mask=None, noise_b=None)
        if _fields is not None:
            _seam_has_nb, _seam_nb, _seam_sp, _, _, _ = _fields
            _seam_rng = np.random.default_rng(tile_seed ^ 0x5C0DEC0)
            _seam_coin = _seam_rng.random((H, W)).astype(np.float32)
            _seam_swap_grid = _seam_coin < _seam_sp
    except Exception:
        # Dither is best-effort — failure doesn't break placement.
        _seam_swap_grid = None

    # Split index into trees and bushes per biome
    def _filter_entries(entries, schem_type):
        return [e for e in entries if e.schem_type == schem_type]

    for pass_type in ("tree", "bush"):
        # Bushes get their own exclusion grid (smaller radius, independent of trees)
        if pass_type == "bush":
            bush_exclusion = _ExclusionGrid(H, W)

        for idx in order:
            row = int(rows_arr[idx])
            col = int(cols_arr[idx])

            biome_str = str(biome_grid[row, col])
            # S59: ecotone seam dither — swap to neighbour biome's entries list
            # at rolled pixels so species mix across biome boundaries.
            # S63: guarded by ECOTONE_DENY_PAIRS — don't swap across aesthetic
            # conflicts (e.g. conifers into tropical jungle).
            if _seam_swap_grid is not None and _seam_swap_grid[row, col]:
                _alt = str(_seam_nb[row, col])
                if (_alt and _alt in index
                        and frozenset({biome_str, _alt}) not in ECOTONE_DENY_PAIRS):
                    biome_str = _alt
            all_entries = index.get(biome_str)
            if not all_entries:
                continue

            entries = _filter_entries(all_entries, pass_type)
            if not entries:
                continue

            # Base density × noise
            base_d = BASE_DENSITY.get(biome_str, 0.05)
            final_d = base_d * float(density_mult[row, col])

            # Eco density modulation
            if eco_density_tile is not None:
                final_d *= float(eco_density_tile[row, col])

            # Bush density scaling: 40% of tree density
            if pass_type == "bush":
                final_d *= 0.4
                if biome_str in SPARSE_BUSH_BIOMES:
                    final_d *= 0.5

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

            # No-repeat rule: reject if same schematic was placed nearby
            entry_path = entry.path
            radius = CANOPY_RADIUS.get(entry.size, 4)
            reject_radius = radius * 2
            is_dupe = False
            for prev in placements[max(0, len(placements)-30):]:
                if prev.schem_path == entry_path:
                    dr = abs(prev.world_z - (py_off + row))
                    dc = abs(prev.world_x - (px_off + col))
                    if dr <= reject_radius and dc <= reject_radius:
                        is_dupe = True
                        break
            if is_dupe:
                # Try one re-roll with different weights (suppress the dupe)
                alt_weights = [w if e.path != entry_path else w * 0.01
                               for e, w in zip(entries, weights)]
                if max(alt_weights) > 0:
                    entry = rng.choices(entries, weights=alt_weights, k=1)[0]

            # Canopy exclusion check
            radius = CANOPY_RADIUS.get(entry.size, 4)
            if pass_type == "tree":
                if not exclusion.is_clear(row, col, radius):
                    continue
            else:
                bush_r = max(1, radius // 2)
                if not bush_exclusion.is_clear(row, col, bush_r):
                    continue

            # Position jitter: ±2 blocks to break grid regularity
            jitter_x = rng.randint(-2, 2)
            jitter_z = rng.randint(-2, 2)
            jittered_col = max(0, min(W - 1, col + jitter_x))
            jittered_row = max(0, min(H - 1, row + jitter_z))

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

            # Random rotation (0, 90, 180, 270 degrees)
            rotation = rng.choice([0, 1, 2, 3])

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
            ))

            # Mark exclusion zone
            if pass_type == "tree":
                exclusion.mark(row, col, radius)
            else:
                bush_exclusion.mark(row, col, max(1, radius // 2))

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
