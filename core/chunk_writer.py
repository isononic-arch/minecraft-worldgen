"""
chunk_writer.py — Step 9: Chunk Writer
Vandir World Generation Pipeline — /core/chunk_writer.py

Responsibilities:
  - Accept all outputs from Steps 6–8 for one tile
  - Write terrain columns (bedrock → stone → subsurface → surface → water/air)
  - Write ground cover (surface_y + 1)
  - Stamp schematics from PlacementRecord list via schematic_loader
  - Write MC biome IDs per chunk column (4×4 block quanta in 1.20.1)
  - Save resulting chunks into .mca region files via amulet

World constants (Higher Heights datapack):
  Y_MIN    = -64   (bedrock layer)
  Y_MAX    = 704   (build height, S84: 512-block world -> 768-block world)
  SEA_Y    = 63    (MC sea level — water fill mandatory below this)
  SECTION_H = 16   (blocks per ChunkSection)

Arch rules:
  - No GUI imports ever
  - All ChunkSection population via NumPy array ops — NO nested block loops
  - amulet used for chunk read/write and region file management
  - schematic_loader used for schematic stamping
  - No full raster loads anywhere in this file
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# WORLD CONSTANTS (Higher Heights datapack — locked)
# ---------------------------------------------------------------------------
Y_MIN     = -64
Y_MAX     = 704   # S84: bumped 448 -> 704 for 768-block world height
SEA_Y     = 63
SECTION_H = 16
Y_RANGE   = Y_MAX - Y_MIN          # 768 (S84: was 512)
N_SECTIONS = Y_RANGE // SECTION_H  # 48 (S84: was 32)

# MC 1.20.1 biome name → internal string (subset used in Vandir)
# Full MC biome IDs assigned by amulet via string name — no numeric IDs needed.
BIOME_TO_MC: dict[str, str] = {
    "COASTAL_HEATH":           "minecraft:savanna_plateau",  # S71: was plains
    "TEMPERATE_RAINFOREST":    "minecraft:dark_forest",
    "BOREAL_TAIGA":            "minecraft:taiga",  # S94e: split off meadow → own tag so the no-snow datapack can give BT snowy_taiga grey-green tint (#80b497) distinct from BA's meadow green. Datapack overrides taiga: temp 1.0 (never snows ≤Y700) + rain. REQUIRES re-render (biome ID baked into .mca).
    "BOREAL_ALPINE":           "minecraft:meadow",   # S86: was plains. Meadow shares BT's MC tag — BA differentiation now lives in palette + ecology, not in MC sky biome. No freeze in lowland BA water.
    "SNOWY_BOREAL_TAIGA":      "minecraft:snowy_taiga",
    "ARCTIC_TUNDRA":           "minecraft:snowy_plains",  # S70: was frozen_peaks (jagged ice spikes); user wants flat snow scrubland
    "FROZEN_FLATS":            "minecraft:badlands",  # S71-3 was swamp — user changed to badlands tint
    "TEMPERATE_DECIDUOUS":     "minecraft:forest",
    "RAINFOREST_COAST":        "minecraft:old_growth_birch_forest",
    "RIPARIAN_WOODLAND":       "minecraft:mangrove_swamp",  # S71-3 was dark_forest
    "DRY_OAK_SAVANNA":         "minecraft:savanna",
    "KARST_BARRENS":           "minecraft:savanna_plateau",
    "BIRCH_FOREST":            "minecraft:birch_forest",
    "EASTERN_TEMPERATE_COAST": "minecraft:beach",
    "MIXED_FOREST":            "minecraft:forest",
    "CONTINENTAL_STEPPE":      "minecraft:cherry_grove",  # S71: was plains
    "DRY_PINE_BARRENS":        "minecraft:wooded_badlands",
    "SCRUBBY_HEATHLAND":       "minecraft:badlands",  # S71: was plains
    "LUSH_RAINFOREST_COAST":   "minecraft:jungle",
    "SAND_DUNE_DESERT":        "minecraft:desert",
    "DESERT_STEPPE_TRANSITION":"minecraft:savanna_plateau",
    "SEMI_ARID_SHRUBLAND":     "minecraft:savanna",
    "DRY_WOODLAND_MAQUIS":     "minecraft:wooded_badlands",  # S70: was sparse_jungle (bright green); user wants timber-badlands dry look
    "TIDAL_JUNGLE_FRINGE":     "minecraft:sparse_jungle",
    "MANGROVE_COAST":          "minecraft:mangrove_swamp",
    "FRESHWATER_FEN":          "minecraft:swamp",
    # Fallback for ocean / unclassified
    "_OCEAN":                  "minecraft:ocean",
    "_DEFAULT":                "minecraft:plains",
}

# S62: sky-biome override — for each Vandir biome in this dict, biome cells
# strictly above the per-column surface Y are painted with this MC biome
# instead of BIOME_TO_MC[biome].  Purpose: stop snow/ice precipitation at
# altitude for biomes whose ground biome is cold (MC temp < 0.5) but which
# are NOT intended to be snowy (e.g. BOREAL_ALPINE → taiga grass tint, no
# snow).  MC queries precipitation biome at MOTION_BLOCKING heightmap
# (surface+1).  With sky=plains (temp 0.8) on the cell containing surface+1,
# no snow accumulates.
#
# Gotcha: MC biome cells are 4×4×4.  When surface_Y and surface_Y+1 land in
# the same 4-block Y cell (75% of Y values), that cell decides both grass
# tint AND precipitation.  Rule we use: cell.bottom_Y >= surface_Y → paint
# as sky.  This guarantees zero snow but means the immediate-surface cell is
# usually plains-tinted.  Taiga tint only retained when surface_Y mod 4 == 3.
#
# Biomes NOT in this dict: sky == ground (no change, current behavior).
BIOME_TO_MC_SKY: dict[str, str] = {
    # S64: emptied — BOREAL_ALPINE now uses wholesale minecraft:plains via BIOME_TO_MC,
    # so the per-section sky-override split is no longer needed.  Kept as a hook for
    # future biomes that need the dual-layer approach.
}

# S67: altitude-based biome remap.  User's rule:
# "if there is another NON snowy biome at a higher value than snowy, we
# should swap out snowy taiga for its non snowy option."
#
# Interpretation: BOREAL_TAIGA (snow-at-altitude) should ALWAYS become
# BOREAL_ALPINE (plains = no snow) — the NON-snowy variant is aesthetically
# equivalent at ground level (same palette + schematic routing) and never
# snows.  Unconditional remap (threshold=-64) keeps the entire biome
# snow-free.  For SNOWY_BOREAL_TAIGA (intentionally snowy), a dither zone
# 200-250 controls snow coverage: pixels in that band with noise > 0.5 get
# the snow, others do not.  Above 250: snowy.  Below 200: no snow unless
# noise hit.  Creates mountaincap look.
BIOME_ALTITUDE_REMAPS: list[dict] = []
# S85: BIOME_ALTITUDE_REMAPS removed entirely.  Pre-S85 this list let high-altitude
# pixels of source biome get the MC tag of target biome (e.g. BIRCH/MIXED → BA at Y>220,
# BT → BA always).  Stale at the 220 threshold for the 768-height world.  Per user S85
# direction "DELET" — MC biome tag now follows Vandir biome exactly via BIOME_TO_MC.
# Runtime code at line ~1607 short-circuits cleanly on empty list (zero cost).

# S67: SNOWY_BOREAL_TAIGA mountaincap dither.  Pixels with surface_y in
# [200, 250] get probabilistic snowy biome based on a coarse simplex noise:
# noise > threshold → keep SNOWY (snow), else → remap to BOREAL_ALPINE.
# Above 250: always snowy.  Below 200: always non-snowy.  Dither scale is
# larger than tile-local variance so you get mountain-cap patches.
SBT_DITHER_MIN_Y = 200
SBT_DITHER_MAX_Y = 250
SBT_DITHER_NOISE_SCALE = 40    # blocks — bigger than local slope variation
SBT_DITHER_THRESHOLD = 0.0     # noise > threshold → keep snowy

# Block name → amulet block string (minecraft: prefix, no namespace stored here)
# amulet uses "minecraft:stone" etc — we store bare names and prepend at write time.
_NS = "minecraft:"

def _b(name: str) -> str:
    """Prepend minecraft: namespace if not already present."""
    return name if name.startswith("minecraft:") else f"{_NS}{name}"


# ---------------------------------------------------------------------------
# GEOLOGICAL STONE TYPES  (per biome — proxy for geological domain until
# geo_domain.tif is available in Phase 3)
# ---------------------------------------------------------------------------

# Primary cliff stone per biome family.  Cliff banding fills the interior of
# steep columns with bands of these variants instead of monotone stone.
_BIOME_CLIFF_STONE: dict[str, str] = {
    # Alpine / arctic — cold metamorphic
    "ARCTIC_TUNDRA":          "andesite",
    "BOREAL_TAIGA":           "andesite",
    "BOREAL_ALPINE":          "andesite",
    "SNOWY_BOREAL_TAIGA":     "andesite",
    "FROZEN_FLATS":           "andesite",
    "COASTAL_HEATH":          "andesite",
    "SCRUBBY_HEATHLAND":      "andesite",
    # Karst / limestone
    "KARST_BARRENS":          "tuff",
    # Desert / arid — sedimentary
    "SAND_DUNE_DESERT":       "sandstone",
    "DESERT_STEPPE_TRANSITION":"sandstone",
    "SEMI_ARID_SHRUBLAND":    "sandstone",
    "DRY_WOODLAND_MAQUIS":    "sandstone",
    "DRY_PINE_BARRENS":       "sandstone",
    "DRY_OAK_SAVANNA":        "sandstone",
    # Granitic highlands
    "MIXED_FOREST":           "granite",
    "BIRCH_FOREST":           "granite",
    "CONTINENTAL_STEPPE":     "granite",
    # Diorite — temperate / ocean
    "TEMPERATE_RAINFOREST":   "diorite",
    "SNOWY_BOREAL_TAIGA":     "diorite",
    "TEMPERATE_DECIDUOUS":    "diorite",
    "RAINFOREST_COAST":       "diorite",
    "LUSH_RAINFOREST_COAST":  "diorite",
    "EASTERN_TEMPERATE_COAST":"diorite",
    # Coastal / riparian — calcite accent
    "RIPARIAN_WOODLAND":      "stone",
    "MANGROVE_COAST":         "stone",
    "FRESHWATER_FEN":         "stone",
    "TIDAL_JUNGLE_FRINGE":    "stone",
}
_DEFAULT_CLIFF_STONE = "stone"

# Banding sequences per primary stone (5 elements, cycled with Y+noise phase)
_CLIFF_BANDS: dict[str, list[str]] = {
    "stone":      ["stone",     "gravel",    "tuff",        "cobblestone", "stone"],
    "andesite":   ["andesite",  "cobblestone","stone",       "gravel",      "andesite"],
    "diorite":    ["diorite",   "stone",     "calcite",     "gravel",      "diorite"],
    "granite":    ["granite",   "stone",     "cobblestone", "gravel",      "granite"],
    "tuff":       ["tuff",      "stone",     "gravel",      "tuff",        "cobblestone"],
    "sandstone":  ["sandstone", "red_sand",  "gravel",      "sandstone",   "sand"],
}


# ---------------------------------------------------------------------------
# BLOCK PALETTE — maps block name strings ↔ uint16 indices.
# Keeps the (512,512,512) volume as uint16 (~256 MB) instead of
# dtype=object (~4.8 GB).  Strings are only materialised for small
# per-chunk (16,16,16) slices at NBT-write time.
# ---------------------------------------------------------------------------

class BlockPalette:
    """Bidirectional map: block-name string ↔ uint16 index."""

    __slots__ = ("_names", "_idx", "_stamp_luts")

    def __init__(self):
        self._names: list[str] = ["air"]   # index 0 is always air
        self._idx: dict[str, int] = {"air": 0}
        self._stamp_luts = None            # S100 stamp-perf: lazy LUT cache

    # -- single lookup --
    def idx(self, name: str) -> int:
        i = self._idx.get(name)
        if i is None:
            i = len(self._names)
            self._names.append(name)
            self._idx[name] = i
        return i

    def name_of(self, index: int) -> str:
        """Return block name for a palette index, or '' if out of range."""
        if 0 <= index < len(self._names):
            return self._names[index]
        return ""

    # -- bulk convert object-string array → uint16 index array --
    def indices(self, names: np.ndarray) -> np.ndarray:
        """Convert an object array of block-name strings to uint16 indices."""
        out = np.empty(names.shape, dtype=np.uint16)
        for uname in np.unique(names):
            s = str(uname)
            out[names == uname] = self.idx(s)
        return out

    # -- bulk convert uint16 index array → object-string array --
    def to_strings(self, indices: np.ndarray) -> np.ndarray:
        lookup = np.array(self._names, dtype=object)
        return lookup[indices.ravel()].reshape(indices.shape)

    @property
    def air(self) -> int:
        return 0

    def fluid_indices(self) -> set[int]:
        """Return set of palette indices that are fluids."""
        return {self._idx[n] for n in ("water", "lava",
                "minecraft:water", "minecraft:lava") if n in self._idx}


# ---------------------------------------------------------------------------
# GEOLOGY HELPERS  (Phase 1.75, S47)
# ---------------------------------------------------------------------------

# Bedrock band: deepslate fills Y_MIN+1 to Y_MIN + _BEDROCK_BAND_DEPTH.
_BEDROCK_BAND_DEPTH = 4

# Sediment block selection thresholds (flow_tile values)
_SEDIMENT_FLOW_HIGH = 0.3    # above → gravel
_SEDIMENT_FLOW_MED  = 0.1    # above → coarse_dirt, below → dirt

# Soil depth by slope class (blocks of dirt/coarse_dirt above sediment)
_SOIL_DEPTH_FLAT     = 4     # slope < 18°
_SOIL_DEPTH_MODERATE = 2     # 18° ≤ slope < 35°
_SOIL_DEPTH_STEEP    = 1     # 35° ≤ slope < 55°
_SOIL_DEPTH_CLIFF    = 0     # slope ≥ 55°

# Maximum sediment thickness (blocks of gravel/dirt above basement)
_SEDIMENT_MAX_BLOCKS = 8


def _compute_xz_waviness(
    H: int, W: int,
    tile_world_x: int, tile_world_z: int,
    band_scale_y: int,
) -> np.ndarray:
    """Compute XZ waviness field (H, W) int32 for tilting band boundaries."""
    # S69: halved from //3 to //6 — band boundaries were oscillating like an EKG
    # trace across tiles; tighter wave keeps strata more columnar.
    wave_amp  = max(1, band_scale_y // 6)
    wave_cell = 32

    row_u = (np.arange(H, dtype=np.int64) + np.int64(tile_world_z)).astype(np.uint32)
    col_u = (np.arange(W, dtype=np.int64) + np.int64(tile_world_x)).astype(np.uint32)

    rci = (row_u // wave_cell).astype(np.int32)
    cci = (col_u // wave_cell).astype(np.int32)
    rf  = ((row_u % wave_cell).astype(np.float32)) / wave_cell
    cf  = ((col_u % wave_cell).astype(np.float32)) / wave_cell

    def _cell_hash(ri: np.ndarray, ci: np.ndarray) -> np.ndarray:
        ri2d = ri[:, None].astype(np.uint32)
        ci2d = ci[None, :].astype(np.uint32)
        h = ri2d * np.uint32(2654435761) ^ ci2d * np.uint32(2246822519)
        h ^= h >> np.uint32(16)
        h *= np.uint32(0x45D9F3B)
        h ^= h >> np.uint32(16)
        return h.astype(np.float32) * np.float32(2.3283064e-10)

    rf2d = rf[:, None]; cf2d = cf[None, :]
    v00 = _cell_hash(rci,     cci    )
    v10 = _cell_hash(rci + 1, cci    )
    v01 = _cell_hash(rci,     cci + 1)
    v11 = _cell_hash(rci + 1, cci + 1)
    smooth = (v00 * (1 - rf2d) * (1 - cf2d)
            + v10 * rf2d       * (1 - cf2d)
            + v01 * (1 - rf2d) * cf2d
            + v11 * rf2d       * cf2d)
    return ((smooth * 2 - 1) * wave_amp).astype(np.int32)


def _build_band_lut(
    n_v: int,
    band_min: int,
    band_max: int,
    lut_size: int,
    seed: int,
) -> np.ndarray:
    """Build a Y → variant index LUT with randomised band thicknesses.

    Returns an int32 array of length ``lut_size`` where each element is a
    variant index in [0, n_v).  Band thicknesses are drawn uniformly from
    [band_min, band_max] inclusive, cycling through variants.
    """
    rng = np.random.default_rng(seed & 0x7FFFFFFFFFFFFFFF)   # S95: non-negative for islands at negative world coords (identity for mainland seeds < 2^63)
    lut = np.empty(lut_size, dtype=np.int32)
    pos = 0
    vi = 0
    while pos < lut_size:
        thickness = int(rng.integers(band_min, band_max + 1))
        end = min(pos + thickness, lut_size)
        lut[pos:end] = vi
        pos = end
        vi = (vi + 1) % n_v
    return lut


def _compute_fault_field(
    H: int, W: int,
    tile_world_x: int, tile_world_z: int,
    fault_scale_blocks: int = 80,
) -> np.ndarray:
    """Smoothed hash-noise field (H, W) float32 in roughly [-1, 1].
    Zero-crossings of this field form continuous curves at world scale —
    used as fault-trace proxy.  Seam-deterministic (world-coord hash)."""
    row_u = (np.arange(H, dtype=np.int64) + np.int64(tile_world_z)).astype(np.uint32)
    col_u = (np.arange(W, dtype=np.int64) + np.int64(tile_world_x)).astype(np.uint32)
    rci = (row_u // fault_scale_blocks).astype(np.int32)
    cci = (col_u // fault_scale_blocks).astype(np.int32)
    rf = ((row_u % fault_scale_blocks).astype(np.float32)) / fault_scale_blocks
    cf = ((col_u % fault_scale_blocks).astype(np.float32)) / fault_scale_blocks

    def _h(ri, ci):
        ri2d = ri[:, None].astype(np.uint32)
        ci2d = ci[None, :].astype(np.uint32)
        h = ri2d * np.uint32(0xC4F12345) ^ ci2d * np.uint32(0x9E373B17)
        h ^= h >> np.uint32(16)
        h *= np.uint32(0x45D9F3B)
        h ^= h >> np.uint32(16)
        return h.astype(np.float32) * np.float32(2.3283064e-10) * 2.0 - 1.0

    rf2 = rf[:, None]; cf2 = cf[None, :]
    v00 = _h(rci,     cci    )
    v10 = _h(rci + 1, cci    )
    v01 = _h(rci,     cci + 1)
    v11 = _h(rci + 1, cci + 1)
    return (v00 * (1 - rf2) * (1 - cf2)
            + v10 * rf2       * (1 - cf2)
            + v01 * (1 - rf2) * cf2
            + v11 * rf2       * cf2).astype(np.float32)


def _compute_vein_field(
    surface_y: np.ndarray,
    tile_world_x: int, tile_world_z: int,
    lap_threshold: float = 4.0,
    fault_scale_blocks: int = 80,
    fault_width: float = 0.08,
) -> np.ndarray:
    """(H, W) bool — True where veins can appear.
    Intersection of (a) terrain Laplacian magnitude > lap_threshold (ridge
    lines + valley floors) and (b) |fault_field| < fault_width (narrow
    band around fault-trace zero-crossings).  Veins concentrate at the
    coincidence of high-curvature terrain and fault zones — geologically
    realistic.  Seam-deterministic."""
    from scipy.ndimage import laplace
    H, W = surface_y.shape
    lap = np.abs(laplace(surface_y.astype(np.float32)))
    fault = _compute_fault_field(H, W, tile_world_x, tile_world_z,
                                   fault_scale_blocks=fault_scale_blocks)
    return (lap > lap_threshold) & (np.abs(fault) < fault_width)


def _apply_banded_fill(
    vol: np.ndarray,
    stone_mask: np.ndarray,
    col_mask: np.ndarray,
    xz_waviness: np.ndarray,
    variant_indices: list[int],
    n_v: int,
    band_scale_y: int,
    col_y_noise: np.ndarray | None = None,
    band_lut: np.ndarray | None = None,
    fleck_probability: float = 0.0,
    fleck_seed: int = 0,
    *,
    speckle_block_idx: int | None = None,
    vein_field_2d: np.ndarray | None = None,
    vein_block_indices: list[int] | None = None,
    vein_amp: float = 0.0,
    vein_seed: int = 0,
) -> None:
    """Apply Y-banded fill to vol for columns matching col_mask, Y-sliced.

    ``col_y_noise`` is an optional (H, W) int array that shifts band boundaries
    per column by ±N blocks, producing organic-looking folded strata.

    ``band_lut`` is an optional precomputed Y → variant-index lookup table with
    randomised band thicknesses (from ``_build_band_lut``).  When provided,
    ``band_scale_y`` is ignored and the LUT is indexed directly.

    S69: ``fleck_probability`` (0.0-0.1) scatters per-voxel random variant swaps
    inside the painted layer — a small % of voxels get overwritten with a
    random other palette block.  Adds subtle salt-and-pepper visual noise to
    otherwise-clean stratified bands.  Default 0.0 (off) — callers must opt in.

    S88: ``speckle_block_idx`` — when set, the fleck swap-target is THIS
    specific block (not a random palette variant).  Used for the strata
    "wear-through to primary rock" effect — speckle is always the primary
    rock_gap block for the lithology.

    S88: ``vein_field_2d`` + ``vein_block_indices`` + ``vein_amp`` + ``vein_seed``
    — after band painting, cells where ``vein_field_2d[z, x]`` is True AND
    a per-cell coin < ``vein_amp`` get overpainted with a block picked
    uniformly at random from ``vein_block_indices`` (1 or 2 entries).
    Produces realistic cross-band veins following ridge/fault geometry.
    Two-block veins look like banded ore (e.g. coal+tuff stripes within
    the same vein trace).
    """
    lut_size = band_lut.shape[0] if band_lut is not None else 0
    SLICE = 32
    _fleck_rng = (
        np.random.default_rng(fleck_seed & 0xFFFFFFFF)
        if fleck_probability > 0 and (n_v > 1 or speckle_block_idx is not None)
        else None
    )
    _SPECKLE_SENTINEL = -1  # used in band_idx to mark per-cell speckle hits
    _vein_active = (
        vein_field_2d is not None
        and vein_block_indices is not None
        and len(vein_block_indices) > 0
        and vein_amp > 0.0
    )
    _vein_rng = (
        np.random.default_rng(vein_seed & 0xFFFFFFFF) if _vein_active else None
    )
    _n_vein_blocks = len(vein_block_indices) if vein_block_indices else 0

    for y_s in range(0, vol.shape[0], SLICE):
        y_e = min(y_s + SLICE, vol.shape[0])
        cs_slice = stone_mask[y_s:y_e] & col_mask[None, :, :]
        if not cs_slice.any():
            continue
        y_arr = np.arange(y_s, y_e, dtype=np.int32)
        y_mod = y_arr[:, None, None] + xz_waviness[None, :, :]
        if col_y_noise is not None:
            y_mod = y_mod + col_y_noise[None, :, :]

        if band_lut is not None:
            # Wrap into LUT range and look up variant index
            band_idx = band_lut[np.abs(y_mod).astype(np.int32) % lut_size]
        else:
            band_idx = (y_mod // band_scale_y) % n_v

        # Flecking / speckle
        if _fleck_rng is not None:
            fleck_coin = _fleck_rng.random(band_idx.shape, dtype=np.float32)
            if speckle_block_idx is not None:
                # S88: swap to a specific block (primary rock_gap "wear-through")
                band_idx = np.where(fleck_coin < fleck_probability,
                                    _SPECKLE_SENTINEL, band_idx)
            elif n_v > 1:
                # Original S69: swap to random palette variant
                fleck_variant = _fleck_rng.integers(0, n_v, size=band_idx.shape,
                                                    dtype=np.int32)
                band_idx = np.where(fleck_coin < fleck_probability,
                                    fleck_variant, band_idx)

        # Per-variant write
        for vi, v_idx in enumerate(variant_indices):
            v_mask = cs_slice & (band_idx == vi)
            if v_mask.any():
                vol[y_s:y_e][v_mask] = v_idx

        # S88 speckle write (sentinel cells)
        if speckle_block_idx is not None:
            sp_mask = cs_slice & (band_idx == _SPECKLE_SENTINEL)
            if sp_mask.any():
                vol[y_s:y_e][sp_mask] = speckle_block_idx

        # S88 vein write — overrides everything else where applicable.
        # Per-cell pick from vein_block_indices (1 or 2 blocks; uniform).
        if _vein_active:
            vein_coin = _vein_rng.random(band_idx.shape, dtype=np.float32)
            vein_apply = (
                cs_slice
                & vein_field_2d[None, :, :]
                & (vein_coin < vein_amp)
            )
            if vein_apply.any():
                if _n_vein_blocks == 1:
                    vol[y_s:y_e][vein_apply] = vein_block_indices[0]
                else:
                    # Per-cell block selection from the 2-element list
                    vein_block_pick = _vein_rng.integers(
                        0, _n_vein_blocks, size=band_idx.shape, dtype=np.int8
                    )
                    for _bi in range(_n_vein_blocks):
                        _bm = vein_apply & (vein_block_pick == _bi)
                        if _bm.any():
                            vol[y_s:y_e][_bm] = vein_block_indices[_bi]


def _apply_strata_fill_v2(
    vol: np.ndarray,
    pal: "BlockPalette",
    stone_mask: np.ndarray,
    col_mask: np.ndarray,
    strata_cfg: dict,
    gid: int,
    *,
    tile_world_x: int,
    tile_world_z: int,
    vein_field_2d: np.ndarray | None = None,
) -> None:
    """S88 walk #4: 2-band mixed strata fill with axis, mix ratios, multi-block speckle/vein.

    Each band is a MIX of (primary, secondary) blocks at primary_pct ratio.
    Strict alternation A-B-A-B via _build_band_lut(n_v=2) (random thickness
    per band, strict block-pair alternation).

    axis:
      "Y_tilted" — Y-based banding with directional tilt + waviness + noise
      "XZ_cols"  — vertical columns; band_idx depends on (x // col_size,
                   z // col_size) hash, constant in Y for each (x,z).

    Multi-block speckle: per-cell uniform pick from speckle_blocks list.
    Multi-block veins: per-cell uniform pick from vein_blocks list, gated
    by vein_field_2d intersect + per-cell coin < vein_amp.
    """
    H, W = stone_mask.shape[-2:]
    axis = strata_cfg.get("axis", "Y_tilted")

    # Resolve block indices (silently skip blocks not in palette)
    def _idx_or_none(name: str) -> int | None:
        try:
            return pal.idx(name)
        except Exception:
            return None

    band_a = strata_cfg.get("band_a", {})
    band_b = strata_cfg.get("band_b", {})
    a_pri = _idx_or_none(band_a.get("primary", "stone")) or pal.idx("stone")
    a_sec = _idx_or_none(band_a.get("secondary", "stone")) or a_pri
    a_pct = float(band_a.get("primary_pct", 50)) / 100.0
    b_pri = _idx_or_none(band_b.get("primary", "stone")) or pal.idx("stone")
    b_sec = _idx_or_none(band_b.get("secondary", "stone")) or b_pri
    b_pct = float(band_b.get("primary_pct", 50)) / 100.0

    speckle_names = strata_cfg.get("speckle_blocks", [])
    speckle_indices = [i for i in (_idx_or_none(n) for n in speckle_names) if i is not None]
    speckle_rate = float(strata_cfg.get("speckle_rate", 0.0))

    vein_names = strata_cfg.get("vein_blocks", [])
    vein_indices = [i for i in (_idx_or_none(n) for n in vein_names) if i is not None]
    vein_amp = float(strata_cfg.get("vein_amp", 0.0))

    # Build band_idx data per axis
    if axis == "XZ_cols":
        col_size = max(1, int(strata_cfg.get("col_size_blocks", 2)))
        col_x = (((np.arange(W, dtype=np.int64) + np.int64(tile_world_x)).astype(np.uint32)) // col_size)
        col_z = (((np.arange(H, dtype=np.int64) + np.int64(tile_world_z)).astype(np.uint32)) // col_size)
        salt_a = np.uint32(0xC4F12345 ^ ((gid * 1779033703) & 0xFFFFFFFF))
        salt_b = np.uint32(0x9E373B17 ^ ((gid * 2654435769) & 0xFFFFFFFF))
        _h = (col_z[:, None] * salt_a) ^ (col_x[None, :] * salt_b)
        _h ^= _h >> np.uint32(16)
        _h *= np.uint32(0x45D9F3B)
        _h ^= _h >> np.uint32(16)
        band_idx_2d = (_h & np.uint32(1)).astype(np.int8)  # (H, W)
        y_offset_2d = None
        band_lut = None
    else:  # Y_tilted
        thickness_min = int(strata_cfg.get("thickness_min", 4))
        thickness_max = int(strata_cfg.get("thickness_max", 10))
        lut_seed = tile_world_x * 73856093 ^ tile_world_z * 19349669 ^ gid * 2654435761
        _LUT_SIZE = Y_RANGE * 2
        band_lut = _build_band_lut(2, thickness_min, thickness_max, _LUT_SIZE, lut_seed)
        tilt_per100 = float(strata_cfg.get("tilt_per_100blocks", 0.0))
        tilt_dir = float(strata_cfg.get("tilt_dir_deg", 0.0))
        noise_amp = int(strata_cfg.get("noise_amp_blocks", 0))
        # Waviness (organic boundaries) — amplitude based on band thickness
        _xz_band_scale = max(8, thickness_max)
        xz_waviness = _compute_xz_waviness(H, W, tile_world_x, tile_world_z, _xz_band_scale)
        if noise_amp > 0:
            _ng_rng = np.random.default_rng(
                (tile_world_x * 73856093 ^ tile_world_z * 19349669 ^ gid * 1234567) & 0xFFFFFFFF
            )
            col_y_noise = _ng_rng.integers(
                -noise_amp, noise_amp + 1, size=(H, W), dtype=np.int32
            )
        else:
            col_y_noise = np.zeros((H, W), dtype=np.int32)
        col_world_x = (np.arange(W, dtype=np.float32) + tile_world_x)
        row_world_z = (np.arange(H, dtype=np.float32) + tile_world_z)
        tilt_rad = np.deg2rad(tilt_dir)
        tilt_offset_2d = (
            (tilt_per100 / 100.0)
            * (col_world_x[None, :] * np.cos(tilt_rad)
               + row_world_z[:, None] * np.sin(tilt_rad))
        ).astype(np.int32)
        y_offset_2d = xz_waviness + col_y_noise + tilt_offset_2d
        band_idx_2d = None

    # Per-cell RNGs (deterministic per tile + group; new seeds per slice)
    base_seed = (tile_world_x * 1779033703 ^ tile_world_z * 0xDEADBEEF ^ gid * 0xCAFED00D) & 0xFFFFFFFF
    primary_rng = np.random.default_rng(base_seed)
    speckle_coin_rng = np.random.default_rng(base_seed ^ 0x12345678)
    speckle_pick_rng = np.random.default_rng(base_seed ^ 0x87654321)
    vein_coin_rng = np.random.default_rng(base_seed ^ 0xABCDEF01)
    vein_pick_rng = np.random.default_rng(base_seed ^ 0x10FEDCBA)

    _LUT_SIZE_LOCAL = Y_RANGE * 2
    SLICE = 32

    for y_s in range(0, vol.shape[0], SLICE):
        y_e = min(y_s + SLICE, vol.shape[0])
        slice_h = y_e - y_s
        cs_slice = stone_mask[y_s:y_e] & col_mask[None, :, :]
        if not cs_slice.any():
            continue

        # band_idx_3d for this Y slice
        if axis == "XZ_cols":
            band_idx_3d = np.broadcast_to(band_idx_2d[None, :, :], (slice_h, H, W))
        else:
            y_arr = np.arange(y_s, y_e, dtype=np.int32)
            y_mod = y_arr[:, None, None] + y_offset_2d[None, :, :]
            band_idx_3d = band_lut[np.abs(y_mod).astype(np.int32) % _LUT_SIZE_LOCAL]

        # Primary/secondary pick per band
        is_band_a = band_idx_3d == 0
        primary_3d = np.where(is_band_a, a_pri, b_pri).astype(np.int32)
        secondary_3d = np.where(is_band_a, a_sec, b_sec).astype(np.int32)
        pct_3d = np.where(is_band_a, a_pct, b_pct).astype(np.float32)
        primary_coin = primary_rng.random(band_idx_3d.shape, dtype=np.float32)
        block_idx = np.where(primary_coin < pct_3d, primary_3d, secondary_3d)
        del is_band_a, primary_3d, secondary_3d, pct_3d, primary_coin

        # Speckle override
        if speckle_rate > 0 and speckle_indices:
            sp_coin = speckle_coin_rng.random(band_idx_3d.shape, dtype=np.float32)
            sp_apply = (sp_coin < speckle_rate) & cs_slice
            if sp_apply.any():
                if len(speckle_indices) == 1:
                    block_idx = np.where(sp_apply, speckle_indices[0], block_idx)
                else:
                    sp_pick = speckle_pick_rng.integers(
                        0, len(speckle_indices), size=band_idx_3d.shape, dtype=np.int8
                    )
                    for _i, _sp_idx in enumerate(speckle_indices):
                        _m = sp_apply & (sp_pick == _i)
                        if _m.any():
                            block_idx = np.where(_m, _sp_idx, block_idx)
                    del sp_pick
            del sp_coin, sp_apply

        # Vein override (highest priority)
        if vein_amp > 0 and vein_indices and vein_field_2d is not None:
            v_coin = vein_coin_rng.random(band_idx_3d.shape, dtype=np.float32)
            v_apply = (v_coin < vein_amp) & vein_field_2d[None, :, :] & cs_slice
            if v_apply.any():
                if len(vein_indices) == 1:
                    block_idx = np.where(v_apply, vein_indices[0], block_idx)
                else:
                    v_pick = vein_pick_rng.integers(
                        0, len(vein_indices), size=band_idx_3d.shape, dtype=np.int8
                    )
                    for _i, _v_idx in enumerate(vein_indices):
                        _m = v_apply & (v_pick == _i)
                        if _m.any():
                            block_idx = np.where(_m, _v_idx, block_idx)
                    del v_pick
            del v_coin, v_apply

        # Write to vol where cs_slice True
        vol[y_s:y_e] = np.where(cs_slice, block_idx, vol[y_s:y_e])
        del block_idx, band_idx_3d


def _fill_geology_layers(
    vol: np.ndarray,
    pal: "BlockPalette",
    stone_mask: np.ndarray,
    abs_y: np.ndarray,
    surface_y: np.ndarray,
    lithology_tile: np.ndarray,
    *,
    flow_tile: np.ndarray | None,
    cfg: dict | None,
    band_scale_y: int,
    tile_world_x: int,
    tile_world_z: int,
) -> None:
    """
    Phase 1.75 geology fill.  Overwrites the stone-filled range
    [Y_MIN+1, surface_y-4] with stratified geology layers:

      1. Bedrock band  (deepslate, Y_MIN+1 to Y_MIN + _BEDROCK_BAND_DEPTH)
      2. Basement rock  (lithology palette with Y-banding, above bedrock band)
      3. Sediment       (gravel/dirt/coarse_dirt by flow, above basement)
      4. Soil horizon   (dirt/coarse_dirt by slope, below surface_y-3)

    Operates in-place on ``vol``.  Only touches voxels where ``stone_mask``
    is True (i.e. the [Y_MIN+1, surface_y-3] range on land columns).
    """
    H, W = surface_y.shape

    # ---- Upscale lithology to full resolution if needed (NEAREST for discrete IDs) ----
    if lithology_tile.shape != (H, W):
        from scipy.ndimage import zoom
        _zh = H / lithology_tile.shape[0]
        _zw = W / lithology_tile.shape[1]
        lithology_tile = zoom(lithology_tile, (_zh, _zw), order=0)  # order=0 = nearest

    # ---- Derive slope from surface_y (smoothed to avoid staircase aliasing) ----
    from core.eco_gradients import compute_cliff_deg
    slope_deg = compute_cliff_deg(surface_y)

    # ---- 1. Compute soil_depth per column: f(slope) ----
    soil_depth = np.full((H, W), _SOIL_DEPTH_FLAT, dtype=np.int32)
    soil_depth[slope_deg >= 18] = _SOIL_DEPTH_MODERATE
    soil_depth[slope_deg >= 35] = _SOIL_DEPTH_STEEP
    soil_depth[slope_deg >= 55] = _SOIL_DEPTH_CLIFF

    # ---- 2. Compute sediment_thickness per column: f(concavity, flow) ----
    # Concavity via second derivatives of surface_y (Laplacian approximation)
    _gy, _gx = np.gradient(surface_y.astype(np.float32))
    _gyy, _ = np.gradient(_gy)
    _, _gxx = np.gradient(_gx)
    concavity_raw = _gyy + _gxx  # positive = concave (valley), negative = convex
    del _gy, _gx, _gyy, _gxx

    concavity_pos = np.clip(concavity_raw, 0, None)
    c_max = np.percentile(concavity_pos[concavity_pos > 0], 95) if np.any(concavity_pos > 0) else 1.0
    concavity_norm = np.clip(concavity_pos / max(c_max, 1e-6), 0, 1).astype(np.float32)

    flow_f = np.clip(flow_tile, 0, 1).astype(np.float32) if flow_tile is not None else np.zeros((H, W), dtype=np.float32)

    # 60% concavity + 40% flow → scale to max blocks
    sed_raw = concavity_norm * 0.6 + flow_f * 0.4
    sediment_thickness = np.clip((sed_raw * _SEDIMENT_MAX_BLOCKS).astype(np.int32),
                                 0, _SEDIMENT_MAX_BLOCKS)

    # ---- 3. Compute layer boundaries (absolute MC Y) ----
    bedrock_band_top_y = Y_MIN + _BEDROCK_BAND_DEPTH         # scalar
    # S87 walk #4 v6: stone_zone_top raised again (was surface_y - 3) to
    # match the 1-dirt-layer sub_blk emission in build_column_array.
    # Geology fill starts immediately below the single dirt layer.
    stone_zone_top     = (surface_y - 2).astype(np.int32)     # (H, W)

    # Top-down allocation within the stone zone:
    #   soil at top → sediment below → basement fills the rest
    soil_top_y = stone_zone_top                                # (H, W)
    soil_bot_y = np.maximum(soil_top_y - soil_depth + 1,
                            bedrock_band_top_y + 1)

    sed_top_y  = soil_bot_y - 1
    sed_bot_y  = np.maximum(sed_top_y - sediment_thickness + 1,
                            bedrock_band_top_y + 1)

    # Basement fills whatever remains between bedrock band and sediment bottom
    # (automatically zero-thickness if the column is too short)

    # ---- 4. Build lithology palette LUT from config ----
    lith_cfg = (cfg or {}).get("lithology", {})
    groups   = lith_cfg.get("groups", {})

    # group_id → list of palette block index (for banding)
    id_to_pal: dict[int, list[int]] = {}
    for gdata in groups.values():
        gid = gdata["id"]
        id_to_pal[gid] = [pal.idx(b) for b in gdata.get("palette", ["stone"])]

    # Fallback for group_id=0 (water/unclassified) or missing groups
    fallback_pal = [pal.idx("stone")]

    # ---- 5. Fill layers ----

    # 5a. Bedrock band → deepslate
    DEEPSLATE_IDX = pal.idx("deepslate")
    bb_mask = stone_mask & (abs_y <= bedrock_band_top_y)
    vol[bb_mask] = DEEPSLATE_IDX

    # 5b. Basement rock with lithology-palette banding.
    # S88: per-group `strata` config overrides hardcoded BAND_MIN/MAX/fleck.
    # When a group has a strata block, use its 3-block palette + thickness
    # range + speckle (palette[0] of the group's main palette) + vein.
    # Without strata config, fall back to the S69 banded-fill behavior.
    _BAND_MIN_DEFAULT = 4
    _BAND_MAX_DEFAULT = 10
    _LUT_SIZE = Y_RANGE * 2  # enough headroom for waviness + noise offsets
    basement_mask = stone_mask & (abs_y > bedrock_band_top_y)

    # Pre-compute vein field ONCE per tile (shared across all groups);
    # per-group vein_amp gates how strongly each lithology uses it.
    _global_strata_cfg = lith_cfg.get("strata", {}) if isinstance(lith_cfg, dict) else {}
    _vein_lap_thr = float(_global_strata_cfg.get("vein_lap_threshold", 4.0))
    _vein_fault_scale = int(_global_strata_cfg.get("vein_fault_scale_blocks", 80))
    _vein_fault_width = float(_global_strata_cfg.get("vein_fault_width", 0.08))
    _vein_field = _compute_vein_field(
        surface_y, tile_world_x, tile_world_z,
        lap_threshold=_vein_lap_thr,
        fault_scale_blocks=_vein_fault_scale,
        fault_width=_vein_fault_width,
    )

    # Pre-compute organic XZ waviness (used by all groups; per-group tilt
    # adds a linear directional offset on top in the loop below).
    _base_waviness = _compute_xz_waviness(H, W, tile_world_x, tile_world_z, band_scale_y)
    _row_world_z = np.arange(H, dtype=np.float32) + tile_world_z
    _col_world_x = np.arange(W, dtype=np.float32) + tile_world_x

    # Per-column Y noise field (will be sized per group from strata.noise_amp_blocks)
    _noise_rng_default = np.random.default_rng((tile_world_x * 73856093 ^ tile_world_z * 19349669) & 0x7FFFFFFFFFFFFFFF)

    for gid, palette_idx_list in id_to_pal.items():
        group_cols = (lithology_tile == gid)  # (H, W)
        if not group_cols.any():
            continue

        # Find this group's name + strata block
        _gname = None
        _gdata = None
        for _gn, _gd in groups.items():
            if _gd.get("id") == gid:
                _gname = _gn
                _gdata = _gd
                break
        _strata = _gdata.get("strata") if _gdata else None

        _lut_seed = tile_world_x * 73856093 ^ tile_world_z * 19349669 ^ gid * 2654435761

        if _strata is not None:
            # ── S88 walk #4 strata v2 path ───────────────────────────────
            # 2-band mixed strata: each band has (primary, secondary, primary_pct).
            # axis: "Y_tilted" (horizontal/tilted bands) or "XZ_cols"
            # (vertical columns).  Multi-block speckle + multi-block veins.
            # Schema validation: must have band_a + band_b dicts.
            if "band_a" in _strata and "band_b" in _strata:
                _apply_strata_fill_v2(
                    vol=vol,
                    pal=pal,
                    stone_mask=basement_mask,
                    col_mask=group_cols,
                    strata_cfg=_strata,
                    gid=gid,
                    tile_world_x=tile_world_x,
                    tile_world_z=tile_world_z,
                    vein_field_2d=_vein_field,
                )
                continue  # skip the default path below
            # If no band_a/band_b -> fall through to default banded
            _strata = None

        # ── Default (S69) banded fill for groups without strata config ────
        col_y_noise = _noise_rng_default.integers(-1, 2, size=(H, W), dtype=np.int32)
        band_lut = _build_band_lut(
            len(palette_idx_list), _BAND_MIN_DEFAULT, _BAND_MAX_DEFAULT,
            _LUT_SIZE, _lut_seed,
        )
        _apply_banded_fill(
            vol, basement_mask, group_cols, _base_waviness,
            palette_idx_list, len(palette_idx_list), band_scale_y,
            col_y_noise=col_y_noise,
            band_lut=band_lut,
            fleck_probability=0.025,
            fleck_seed=tile_world_x * 83492791 ^ tile_world_z * 46508633 ^ gid * 1779033703,
        )

    # Columns with lithology_tile==0 (water/unclassified) keep stone (already filled)

    # S87 walk #4 v5: SOIL + SEDIMENT layers DISABLED.
    # Per user (36,15) spec: at y <= surface_y - 3, want PURE lithology stone
    # (calcite/granite/etc.) with no dirt/sediment intercept.  The 2 dirt
    # blocks from chunk_writer sub_blk (sy-1, sy-2) provide the topsoil; below
    # that, basement_mask + cliff_banded lithology palette fills the rest.
    # Sediment + soil writes removed; basement (computed above) fills the
    # entire stone_zone naturally.


# ---------------------------------------------------------------------------
# BLOCK ARRAY BUILDER  (pure NumPy — no amulet dependency for building)
# ---------------------------------------------------------------------------

def build_column_array(
    surface_y:    np.ndarray,    # (H, W) int16
    surface_blk:  np.ndarray,    # (H, W) str — surface block name
    sub_blk:      np.ndarray,    # (H, W) str — subsurface block name
    ground_cover: np.ndarray,    # (H, W) str — block at surface_y+1 ('' = air)
    biome_grid:   np.ndarray | None = None,  # (H, W) str — for cliff stone type
    cliff_deg_thr: float = 45.0,             # degrees — gradient threshold for banding
    band_scale_y:  int   = 12,               # Y blocks per band cycle
    tile_world_x:  int   = 0,
    tile_world_z:  int   = 0,
    river_water_y: np.ndarray | None = None, # (H, W) int16 — water surface for rivers above sea level
    # ---- Phase 1.5 / 1.75 lithology kwargs (S46-S47) ----
    # When ``use_new_geology`` is True AND ``lithology_tile`` is provided, the
    # function fills the subsurface with geology-stratified layers instead of
    # uniform stone + cliff banding.  See PHYSICAL_REALISM_REFACTOR.md §11
    # Phase 1.75 and §6 Pass 1.
    lithology_tile:          np.ndarray | None = None,  # (H, W) uint8 — lithology group id per column
    sediment_thickness_tile: np.ndarray | None = None,  # (H, W) uint8 — blocks of sediment (unused; inline now)
    soil_horizon_depth_tile: np.ndarray | None = None,  # (H, W) uint8 — blocks of soil (unused; inline now)
    use_new_geology:         bool                = False,
    flow_tile:               np.ndarray | None = None,  # (H, W) float32 [0,1] — for inline sediment thickness
    cfg:                     dict | None        = None,  # thresholds.json — for lithology group palettes
    gap_mask:                np.ndarray | None  = None,  # (H, W) uint8 — for walk #9 rock_zone Y-2..Y-5 cleanup
    river_meta:              np.ndarray | None  = None,  # (H, W) uint8 — channel type (3=CHAN_LAKE) for steep-void lake exclusion
) -> tuple[np.ndarray, "BlockPalette"]:
    """
    Build a (Y_RANGE, H, W) object array of block name strings.

    Layout:
      Y_MIN      → 'bedrock'
      Y_MIN+1 .. surface_y-4  → 'stone' (geology/lithology — buried 1 extra block)
      surface_y-3, surface_y-2, surface_y-1 → sub_blk
      surface_y                → surface_blk
      surface_y+1              → ground_cover (if not '')
      surface_y+1 .. SEA_Y    → 'water'  (only if surface_y < SEA_Y)
      SEA_Y+1 .. Y_MAX-1      → 'air'

    Returns (vol, palette) where vol has shape (Y_RANGE, H, W) dtype=uint16
    and palette is a BlockPalette mapping indices ↔ block name strings.
    """
    # Phase 1.5 compat: unused kwargs kept to avoid breaking existing callers
    _ = (sediment_thickness_tile, soil_horizon_depth_tile)

    H, W = surface_y.shape
    pal = BlockPalette()
    vol = np.zeros((Y_RANGE, H, W), dtype=np.uint16)  # 0 = air

    # Bedrock layer
    vol[0, :, :] = pal.idx("bedrock")

    # Stone fill: Y_MIN+1 .. surface_y-3 (vectorised)
    # Build Y index array once
    y_idx = np.arange(Y_RANGE, dtype=np.int16)[:, None, None]  # (Y, 1, 1)
    abs_y = y_idx + Y_MIN                                        # absolute MC Y

    surf_broad = surface_y[None, :, :]                           # (1, H, W)

    # S87 walk #4 v6: lithology starts at surface_y - 2 since sub_blk only
    # emits 1 layer now (sy-1).
    stone_mask  = (abs_y >= Y_MIN + 1) & (abs_y <= surf_broad - 2)
    water_mask  = (abs_y > surf_broad) & (abs_y <= SEA_Y)

    STONE_IDX = pal.idx("stone")
    vol[stone_mask] = STONE_IDX

    # ---- Subsurface fill: geology branch OR legacy cliff banding ----
    if use_new_geology and lithology_tile is not None:
        _fill_geology_layers(
            vol, pal, stone_mask, abs_y, surface_y, lithology_tile,
            flow_tile=flow_tile, cfg=cfg,
            band_scale_y=band_scale_y,
            tile_world_x=tile_world_x, tile_world_z=tile_world_z,
        )
    elif biome_grid is not None:
        # ---- Legacy cliff interior banding ----
        # For steep columns (cliff_deg >= cliff_deg_thr) replace the uniform stone
        # fill with geologically-keyed banded variants (Y × XZ-hash modulo).
        # Processed in Y-slices of 32 to cap peak memory at ~24 MB per slice.
        from core.eco_gradients import compute_cliff_deg
        cliff_deg = compute_cliff_deg(surface_y)
        cliff_mask = (cliff_deg >= cliff_deg_thr) & (surface_y > SEA_Y)
        del cliff_deg

        if cliff_mask.any():
            xz_waviness = _compute_xz_waviness(
                H, W, tile_world_x, tile_world_z, band_scale_y,
            )

            # Build per-biome stone-type grid
            stone_type_grid = np.full((H, W), _DEFAULT_CLIFF_STONE, dtype=object)
            for _bname in np.unique(biome_grid):
                _bm = (biome_grid == _bname)
                stone_type_grid[_bm] = _BIOME_CLIFF_STONE.get(str(_bname), _DEFAULT_CLIFF_STONE)

            unique_stones = np.unique(stone_type_grid[cliff_mask])

            for prim_stone in unique_stones:
                prim_stone = str(prim_stone)
                variants = _CLIFF_BANDS.get(prim_stone, _CLIFF_BANDS["stone"])
                n_v = len(variants)
                variant_indices = [pal.idx(v) for v in variants]

                col_mask = cliff_mask & (stone_type_grid == prim_stone)  # (H, W)
                if not col_mask.any():
                    continue

                _apply_banded_fill(
                    vol, stone_mask, col_mask, xz_waviness,
                    variant_indices, n_v, band_scale_y,
                )

    # Subsurface, surface, and ground cover — vectorised advanced indexing.
    # Each write targets a specific (Y, row, col) cell determined by surface_y.
    r_idx = np.repeat(np.arange(H, dtype=np.int32), W)   # (H*W,)
    c_idx = np.tile  (np.arange(W, dtype=np.int32), H)   # (H*W,)
    sy_flat   = surface_y.ravel().astype(np.int32)        # (H*W,)

    # S81 v8.5 Issue 4: swap grass_block / podzol / mycelium to dirt for
    # underwater cells. When the carver drops surface_y below river_water_y,
    # surface_decorator may have already painted vegetated surface blocks
    # per biome. They look wrong underwater (e.g. green grass at bottom of
    # a river). Swap to dirt for natural riverbed appearance.
    if river_water_y is not None:
        _underwater_grass_like = (
            np.isin(surface_blk, ("grass_block", "podzol", "mycelium",
                                    "moss_block", "rooted_dirt", "coarse_dirt"))
            & (river_water_y > surface_y)
            & (river_water_y > SEA_Y)
        )
        if _underwater_grass_like.any():
            surface_blk = surface_blk.copy()
            surface_blk[_underwater_grass_like] = "dirt"

    # S87 walk #4 REVERTED: steep-cliff lithology-surface override.
    # The change replaced only surface_blk at slope>=55deg with the lithology
    # palette[0] -- but left sub_blk and the column below as their normal
    # values, producing a "frosting" effect: calcite surface, stone subsurface,
    # mixed below.  User: "replaced exclusively the surface block but KEPT
    # the stone".  Proper fix requires ALSO setting sub_blk at cliff pixels
    # to the lithology palette so the cliff is uniform lithology all the way
    # down.  Deferred to a focused S88 session.

    # Convert string arrays to palette indices
    surf_idx_flat = pal.indices(surface_blk).ravel()
    sub_idx_flat  = pal.indices(sub_blk).ravel()
    cov_flat      = ground_cover.ravel()  # keep as strings for empty check + double-tall logic

    # surface (sy)
    yi_surf = sy_flat - Y_MIN
    valid = (yi_surf >= 0) & (yi_surf < Y_RANGE)
    vol[yi_surf[valid], r_idx[valid], c_idx[valid]] = surf_idx_flat[valid]

    # subsurface sy-1
    yi_sub1 = sy_flat - 1 - Y_MIN
    valid1 = (yi_sub1 >= 0) & (yi_sub1 < Y_RANGE)
    vol[yi_sub1[valid1], r_idx[valid1], c_idx[valid1]] = sub_idx_flat[valid1]

    # S87 walk #4 v6: dropped sy-2 sub-layer too.  Spec: 1 surface + 1 dirt,
    # then lithology takes over at sy-2 and below.  Pure lithology stone there
    # (calcite for karst, granite for granitic, etc.) via _fill_geology_layers.

    # S69: Kill any seagrass/kelp that would pop above the water surface.
    # Root cause: tall_seagrass at surface_y=62 (depth=1) places upper half at
    # Y=64, one block above SEA_Y.  Also defensive against any seagrass/kelp
    # placed where surface_y >= SEA_Y (shouldn't happen per ocean_decorator
    # gating, but cheap to guard).  Mutates ground_cover in place so the stamp
    # + double-tall + uveg-carve passes below all see the cleaned field.
    _kill_tall = (ground_cover == "tall_seagrass") & (surface_y + 2 > SEA_Y)
    if _kill_tall.any():
        ground_cover[_kill_tall] = ""
    _kill_short = np.isin(
        ground_cover, ("seagrass", "kelp", "sea_pickle")
    ) & (surface_y + 1 > SEA_Y)
    if _kill_short.any():
        ground_cover[_kill_short] = ""
    # S84: Narrow the S70 coast-edge veg cleanup. Previously this killed
    # terrestrial ground cover EVERYWHERE surface_y <= SEA_Y — bald-ifying
    # vast coastal bands (every land cell at Y=63 lost its grass/flowers).
    # Now requires the cell to be literally adjacent (within 1 block) to a
    # cell whose surface is BELOW sea level (i.e., an actual water-bearing
    # cell). Preserves inland-but-low coastal vegetation; still kills the
    # literal beach edge that's flush with ocean.
    from scipy.ndimage import binary_dilation as _bin_dilation
    _water_plants = ("seagrass", "tall_seagrass", "kelp", "sea_pickle")
    _below_sea = surface_y < SEA_Y
    _adj_to_water = _bin_dilation(_below_sea, iterations=1)
    _kill_terrestrial = (
        (ground_cover != "")
        & ~np.isin(ground_cover, _water_plants)
        & (surface_y <= SEA_Y)
        & _adj_to_water
    )
    if _kill_terrestrial.any():
        ground_cover[_kill_terrestrial] = ""
    # S81 v8.4 Issue 2: kill ALL ground cover on water-zone cells PLUS a
    # 1-cell shore buffer. Without this, river-edge cells where water_y
    # was set in the broad sigmoid footprint but surface didn't drop
    # below water_y leave grass at sy+1 visually adjacent to the river
    # surface — looks like grass floating on water from any low angle.
    # Buffer covers cells just outside the painted footprint that would
    # otherwise grow vegetation at the visible water surface elevation.
    if river_water_y is not None:
        from scipy.ndimage import binary_dilation as _bd_water_buf
        _water_zone = river_water_y > 0
        _kill_at_water = _bd_water_buf(_water_zone, iterations=1)
        if _kill_at_water.any():
            _kill_terrestrial_water = (
                (ground_cover != "")
                & ~np.isin(ground_cover, _water_plants)
                & _kill_at_water
            )
            if _kill_terrestrial_water.any():
                ground_cover[_kill_terrestrial_water] = ""
    cov_flat = ground_cover.ravel()

    # ground cover sy+1 (only where non-empty)
    cover_mask = np.array([bool(b) for b in cov_flat])
    if cover_mask.any():
        yi_cov = sy_flat[cover_mask] + 1 - Y_MIN
        r_cov  = r_idx[cover_mask]
        c_cov  = c_idx[cover_mask]
        valid_c = (yi_cov >= 0) & (yi_cov < Y_RANGE)
        cov_idx_masked = pal.indices(cov_flat[cover_mask].reshape(-1)).ravel()
        vol[yi_cov[valid_c], r_cov[valid_c], c_cov[valid_c]] = cov_idx_masked[valid_c]

        # Double-tall plants: place [half=upper] at sy+2 for tall_grass, large_fern, etc.
        # S64: tall_seagrass added for underwater 2-block seagrass.
        _DOUBLE_TALL = frozenset({
            "tall_grass", "large_fern",
            "sunflower", "peony", "rose_bush", "lilac", "pitcher_plant",
            "tall_seagrass",
        })
        cov_active = cov_flat[cover_mask][valid_c]
        is_double = np.array([str(b) in _DOUBLE_TALL for b in cov_active])
        if is_double.any():
            yi_top = yi_cov[valid_c][is_double] + 1  # sy+2
            r_top  = r_cov[valid_c][is_double]
            c_top  = c_cov[valid_c][is_double]
            valid_t = (yi_top >= 0) & (yi_top < Y_RANGE)
            # Upper half block names: same block ID with [half=upper] state
            upper_indices = np.array([pal.idx(str(b) + "[half=upper]") for b in cov_active[is_double]], dtype=np.uint16)
            vol[yi_top[valid_t], r_top[valid_t], c_top[valid_t]] = upper_indices[valid_t]
            # Also mark lower half with [half=lower] state
            lower_indices = np.array([pal.idx(str(b) + "[half=lower]") for b in cov_active[is_double]], dtype=np.uint16)
            vol[yi_cov[valid_c][is_double][valid_t],
                r_cov[valid_c][is_double][valid_t],
                c_cov[valid_c][is_double][valid_t]] = lower_indices[valid_t]

    # Water fill (vectorised — covers all sub-sea columns at once)
    WATER_IDX = pal.idx("water")

    # S64: underwater-vegetation survival — carve ground_cover cells out of
    # water_mask BEFORE the fill so seagrass/kelp/sea_pickle aren't overwritten.
    _UNDERWATER_VEG = frozenset({"seagrass", "tall_seagrass", "kelp", "sea_pickle"})
    has_uveg = np.zeros(ground_cover.shape, dtype=bool)
    for _veg in _UNDERWATER_VEG:
        has_uveg |= (ground_cover == _veg)
    if has_uveg.any():
        uveg_rows, uveg_cols = np.where(has_uveg)
        # sy+1 (ground_cover placement)
        uveg_yi1 = surface_y[uveg_rows, uveg_cols].astype(np.int32) + 1 - Y_MIN
        v1 = (uveg_yi1 >= 0) & (uveg_yi1 < Y_RANGE)
        water_mask[uveg_yi1[v1], uveg_rows[v1], uveg_cols[v1]] = False
        # Also carve sy+2 for tall_seagrass (upper half)
        is_tall = ground_cover[uveg_rows, uveg_cols] == "tall_seagrass"
        if is_tall.any():
            tall_yi2 = uveg_yi1 + 1
            v2 = (tall_yi2 >= 0) & (tall_yi2 < Y_RANGE) & is_tall
            water_mask[tall_yi2[v2], uveg_rows[v2], uveg_cols[v2]] = False

    vol[water_mask] = WATER_IDX

    # S64: kelp column stamping — replace water cells from sy+1 up to top with
    # kelp_plant, and kelp (mature) at the top.  Only where ground_cover=="kelp".
    is_kelp = (ground_cover == "kelp")
    if is_kelp.any():
        try:
            KELP_PLANT_IDX = pal.idx("kelp_plant")
            KELP_TOP_IDX   = pal.idx("kelp[age=25]")
        except Exception:
            KELP_PLANT_IDX = pal.idx("kelp")
            KELP_TOP_IDX   = pal.idx("kelp")
        kr, kc = np.where(is_kelp)
        # Per-column RNG for stalk height
        _kelp_rng = np.random.default_rng(
            ((tile_world_x * 7919) ^ (tile_world_z * 31337) ^ 0xC0FFEE) & 0x7FFFFFFFFFFFFFFF
        )
        # S98: taller, config-driven plumes (match the generator's kelp forests).
        # Always capped 1 block below sea (top_y) so they never breach the surface;
        # in deep water they read as tall plumes, in shallow water they stay short.
        _veg_cfg = (cfg or {}).get("ocean", {}).get("vegetation", {})
        _kmin = int(_veg_cfg.get("kelp_min", 7))
        _kmax = max(_kmin + 1, int(_veg_cfg.get("kelp_max", 24)))
        stalk_heights = _kelp_rng.integers(_kmin, _kmax + 1, size=len(kr))
        # S98 perf: vectorised column stamping (was a per-column Python double-loop
        # that dragged on dense/deep-ocean tiles). Build the ragged set of plant-cell
        # (yi, r, c) in one shot via a repeat/cumsum ramp, then scatter the mature tops.
        # Byte-identical to the loop: same RNG draw order, same SEA_Y-1 cap + guards.
        sy_base = surface_y[kr, kc].astype(np.int64)
        top_y = np.minimum(sy_base + stalk_heights, SEA_Y - 1)   # mature-kelp world Y
        stalk_lo = sy_base + 1                                    # first plant world Y
        valid = (sy_base < SEA_Y) & (top_y > stalk_lo)           # underwater + >=1 plant cell
        if valid.any():
            krv, kcv = kr[valid], kc[valid]
            lo = (stalk_lo[valid] - Y_MIN)                       # first plant yi
            hi = (top_y[valid] - Y_MIN)                          # mature yi (= last plant yi + 1)
            lens = hi - lo                                       # plant cells per column (>=1)
            total = int(lens.sum())
            col = np.repeat(np.arange(len(krv)), lens)           # plant cell -> column index
            within = np.arange(total) - np.repeat(np.cumsum(lens) - lens, lens)
            yi_p = np.repeat(lo, lens) + within                  # plant-cell yi
            m = (yi_p >= 0) & (yi_p < Y_RANGE)
            vol[yi_p[m], krv[col][m], kcv[col][m]] = KELP_PLANT_IDX
            mt = (hi >= 0) & (hi < Y_RANGE)
            vol[hi[mt], krv[mt], kcv[mt]] = KELP_TOP_IDX

    # River water fill — above-sea-level rivers need water from carved
    # surface up to river_water_y (1 block below original terrain)
    if river_water_y is not None:
        rw_broad = river_water_y[None, :, :]  # (1, H, W)
        river_water_mask = (
            (abs_y > surf_broad)
            & (abs_y <= rw_broad)
            & (rw_broad > SEA_Y)  # only for above-sea rivers
        )
        vol[river_water_mask] = WATER_IDX

        # Single-block seagrass on riverbed — depth >= 2 only (needs water above)
        # Written AFTER water fill.  Single block only — no tall_seagrass.
        river_px = (river_water_y > SEA_Y) & (river_water_y > surface_y)
        SEAGRASS_IDX = pal.idx("seagrass")
        if river_px.any():
            _rr = np.arange(H, dtype=np.int64).reshape(-1, 1)
            _cc = np.arange(W, dtype=np.int64).reshape(1, -1)
            _sg_hash = ((_rr * 48271 + tile_world_x) ^ (_cc * 16807 + tile_world_z)) & 0xFF
            _sg_norm = _sg_hash / 255.0
            water_depth = (river_water_y - surface_y).astype(np.int32)

            # S89 walk fix: ~20% of river pixels with depth >= 1 get seagrass.
            # Was depth >= 2 + "water above" — but above-sea river channels are
            # mostly 1 block deep, so the old gate excluded ~all rivers and they
            # read as bare. Seagrass is a submerged 1-block plant, so it's valid
            # in the bed-level water block itself (depth 1). Place it where the
            # bed-top block (surface_y+1) is actually river water.
            sg_place = river_px & (_sg_norm < 0.20) & (water_depth >= 1)
            sg_rows, sg_cols = np.where(sg_place)
            # S100 perf: vectorised (was a per-cell scalar loop).  Cells are
            # unique (row,col) pairs so writes never interact; same bounds
            # guard + same water-at-bed check — byte-identical result.
            if len(sg_rows):
                _bed_yi = surface_y[sg_rows, sg_cols].astype(np.int64) + 1 - Y_MIN
                _ok = (_bed_yi >= 0) & (_bed_yi < Y_RANGE)
                _sr, _sc, _by = sg_rows[_ok], sg_cols[_ok], _bed_yi[_ok]
                _hit = vol[_by, _sr, _sc] == WATER_IDX
                vol[_by[_hit], _sr[_hit], _sc[_hit]] = SEAGRASS_IDX

        # Safety: ensure no air pockets in river water columns
        # Any non-water, non-seagrass block between surface and water_y → water
        if river_px.any():
            rp_rows, rp_cols = np.where(river_px)
            # S100 perf: vectorised via a Y-band mask (was a per-column
            # per-Y scalar loop).  Columns are unique (row,col) pairs and
            # each write targets its own (yi,row,col) cell, so batching is
            # order-independent.  Columns whose start index would be
            # negative (surface_y < Y_MIN - 1; never happens in practice)
            # keep the original scalar loop because Python's negative
            # indexing semantics there can't be reproduced by a masked
            # range — preserves exact legacy behaviour.
            _sy_i = surface_y[rp_rows, rp_cols].astype(np.int64) + 1 - Y_MIN
            _wy_i = river_water_y[rp_rows, rp_cols].astype(np.int64) - Y_MIN
            _neg = _sy_i < 0
            if _neg.any():
                for r, c in zip(rp_rows[_neg], rp_cols[_neg]):
                    sy_i = int(surface_y[r, c]) + 1 - Y_MIN
                    wy_i = int(river_water_y[r, c]) - Y_MIN
                    for yi in range(sy_i, min(wy_i + 1, Y_RANGE)):
                        blk_idx = vol[yi, r, c]
                        if blk_idx != WATER_IDX and blk_idx != SEAGRASS_IDX:
                            vol[yi, r, c] = WATER_IDX
            _pos = ~_neg
            if _pos.any():
                _rr2 = rp_rows[_pos]; _cc2 = rp_cols[_pos]
                _lo = _sy_i[_pos]
                _hi = np.minimum(_wy_i[_pos] + 1, Y_RANGE)   # exclusive end
                _nonempty = _hi > _lo
                _rr2, _cc2 = _rr2[_nonempty], _cc2[_nonempty]
                _lo, _hi = _lo[_nonempty], _hi[_nonempty]
                if len(_rr2):
                    _y0 = int(_lo.min()); _y1 = int(_hi.max())
                    _band = vol[_y0:_y1, _rr2, _cc2]          # (band_h, n) copy
                    _yy = np.arange(_y0, _y1, dtype=np.int64)[:, None]
                    _fix = ((_yy >= _lo[None, :]) & (_yy < _hi[None, :])
                            & (_band != WATER_IDX) & (_band != SEAGRASS_IDX))
                    if _fix.any():
                        _fy, _fk = np.nonzero(_fix)
                        vol[_y0 + _fy, _rr2[_fk], _cc2[_fk]] = WATER_IDX

    # ── steep-river STRUCTURE_VOID seal (S94d MASK rework) ─────────────────
    # On steep slopes MC's fluid update cascades the placed river water and it
    # renders ugly (flowing waterfalls down the terraced steps).  structure_void
    # blocks fluid AND has no collision box (invisible + swimmable), so we wall
    # the EXPOSED LATERAL FACES of the river water inside the steep zone with it,
    # turning the cascade into a frozen, contained, invisible-dammed channel.
    #
    # The S93 per-cell version (e1b8321) only sealed cells where a neighbour's
    # water sat >=2 above the column top; the STEEPEST cascades still flowed
    # because that test left gaps along multi-step staircases.  This version
    # builds a connected steep-zone MASK and seals EVERY air cell laterally
    # adjacent to a river-water block within it — a full one-cell void shell
    # hugging the whole cascade.  The bed below the bottom water block is solid
    # stone, so a lateral void shell + solid floor = the water body is fully
    # contained and every source block stays put (no flow target -> no update).
    #
    # Steep-zone seed = terrain slope >= _STEEP_DEG  OR  a real >=_STEP_BLK
    # water-surface drop to a lower river neighbour (the direct cascade signal);
    # dilated _ZONE_DIL so the seed grows to cover the cascade + the neighbour
    # air cells where the void actually lands.  Flat/contained rivers have a
    # level water surface (no exposed lateral faces) and fall outside the zone,
    # so they are untouched.
    #
    # Exclusions: OCEAN outlets (column top <= SEA) stay open so river->ocean
    # deltas drain; LAKE cells (river_meta == CHAN_LAKE, dilated 2px to clear
    # the 2px lake bank) stay open so a steep river->lake cascade pours into the
    # lake instead of being walled into a floating edge (51,53 risk).
    if river_water_y is not None:
        from scipy.ndimage import binary_dilation as _bd_void
        from core.eco_gradients import compute_cliff_deg as _ccd_void
        _CHAN_LAKE = 3
        _STEEP_DEG = 22.0    # terrain-slope gate (deg) for the steep zone
        _STEP_BLK  = 2       # water-surface drop (blocks) counted as a cascade step
        _ZONE_DIL  = 4       # grow seed to cover cascade + neighbour-air targets
        _SVOID = pal.idx("structure_void")

        _ry32 = river_water_y.astype(np.int32)
        _sy32 = surface_y.astype(np.int32)
        # above-sea river/stream water cells (lakes excluded below)
        _rmask = (_ry32 > SEA_Y) & (_ry32 > _sy32)
        if river_meta is not None:
            _lake2d = (np.asarray(river_meta) == _CHAN_LAKE)
            _rmask &= ~_lake2d
        else:
            _lake2d = np.zeros_like(_rmask)

        if _rmask.any():
            # steep-zone seed: terrain-steep river OR a real water step down
            _slope = _ccd_void(surface_y)
            _seed = _rmask & (_slope >= _STEEP_DEG)
            _wl = np.where(_rmask, _ry32, 1 << 20)  # sentinel-high for non-river
            _nbr_min = _wl.copy()
            for _dz, _dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                _nbr_min = np.minimum(_nbr_min, np.roll(np.roll(_wl, _dz, 0), _dx, 1))
            _seed |= _rmask & ((_ry32 - _nbr_min) >= _STEP_BLK)
            _zone = _bd_void(_seed, iterations=_ZONE_DIL)

            # seal-target footprint: in-zone, column above SEA (no ocean), not lake
            _ctop = np.where(_rmask, np.maximum(_sy32, _ry32), _sy32)
            _lake_keepout = _bd_void(_lake2d, iterations=2) if _lake2d.any() else _lake2d
            _seal_ok = _zone & (_ctop > SEA_Y) & (~_lake_keepout)

            if _seal_ok.any():
                # restrict the 3D work to the river-water Y band (perf)
                _lo = max(0, int(_sy32[_rmask].min()) - Y_MIN)
                _hi = min(Y_RANGE, int(_ry32[_rmask].max()) - Y_MIN + 2)
                _sub = vol[_lo:_hi]   # view — boolean assignment writes through
                _absy = (np.arange(_lo, _hi, dtype=np.int32)[:, None, None] + Y_MIN)
                # river-water blocks (above SEA, in river columns)
                _rw3d = (_sub == WATER_IDX) & _rmask[None, :, :] & (_absy > SEA_Y)
                # air cells laterally adjacent to a river-water block at same Y
                _adj = np.zeros_like(_rw3d)
                for _dz, _dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    _adj |= np.roll(np.roll(_rw3d, _dz, 1), _dx, 2)
                _seal3d = _adj & (_sub == 0) & _seal_ok[None, :, :]
                _nv = int(_seal3d.sum())
                if _nv:
                    _sub[_seal3d] = _SVOID
                    print(f"[s93-void-seal] tile=({tile_world_x // 512},"
                          f"{tile_world_z // 512}) steep-zone {int(_zone.sum())} "
                          f"cols, sealed {_nv} river-water lateral air cells",
                          flush=True)

    # ── Floating vegetation cleanup ──────────────────────────────────────
    # Remove any grass-type ground_cover block (at sy+1) that does NOT have a
    # solid support block directly below it. Cases:
    #   (1) block at sy is water → river-bank floater (pre-S60 check)
    #   (2) block at sy is air   → carved terrain / schematic gap (S60 add)
    # Exempts lily_pad implicitly (not in the veg set).
    AIR_IDX = pal.air
    _VEGETATION_BLOCKS = frozenset({
        "short_grass", "tall_grass", "tall_grass[half=lower]",
        "tall_grass[half=upper]", "fern", "large_fern",
        "large_fern[half=lower]", "large_fern[half=upper]",
        "tall_dry_grass", "short_dry_grass",
        "dead_bush", "bush", "azalea", "flowering_azalea", "seagrass",
    })
    veg_indices = frozenset(pal._idx.get(n, -1) for n in _VEGETATION_BLOCKS) - {-1}
    if veg_indices and cover_mask.any():
        # S100 perf: vectorised (was a 262k-iteration scalar loop over every
        # tile pixel).  (row,col) pairs are unique, and each iteration only
        # ever touches its OWN column (reads at sy / sy+1 / sy+2, writes at
        # sy+1 / sy+2), so batch order matches loop order exactly.  Set
        # membership becomes a boolean LUT over palette indices.
        _veg_lut = np.zeros(len(pal._names), dtype=bool)
        _veg_lut[np.fromiter(veg_indices, dtype=np.int64)] = True
        _sy_i_all = sy_flat.astype(np.int64) - Y_MIN
        _cov_yi_all = _sy_i_all + 1
        _okc = (_cov_yi_all >= 0) & (_cov_yi_all < Y_RANGE) & (_sy_i_all >= 0)
        _rv, _cv = r_idx[_okc], c_idx[_okc]
        _syv, _covv = _sy_i_all[_okc], _cov_yi_all[_okc]
        _below = vol[_syv, _rv, _cv]
        _floater = (_veg_lut[vol[_covv, _rv, _cv]]
                    & ((_below == WATER_IDX) | (_below == AIR_IDX)))
        if _floater.any():
            _fr, _fc, _fy = _rv[_floater], _cv[_floater], _covv[_floater]
            vol[_fy, _fr, _fc] = AIR_IDX
            # Also clear upper half if double-tall
            _up = _fy + 1
            _upok = _up < Y_RANGE
            _ur, _uc, _uy = _fr[_upok], _fc[_upok], _up[_upok]
            _upveg = _veg_lut[vol[_uy, _ur, _uc]]
            vol[_uy[_upveg], _ur[_upveg], _uc[_upveg]] = AIR_IDX

    # ── Walk #9 NEW: rock_zone Y-2..Y-5 cleanup ──────────────────────────
    # On rock_gap (gap_mask==5) pixels, force the top-6 column blocks
    # (Y, Y-1, ..., Y-5) to be lithology rock if any grass/dirt slipped
    # through.  Y and Y-1 are handled by surface_decorator; this pass
    # handles Y-2 through Y-5 (the basement strata zone).  Overwrites
    # any GRASS/DIRT block in those rows with the lithology palette[0]
    # (dominant rock for that group).
    if gap_mask is not None and lithology_tile is not None and cfg is not None:
        _cu = cfg.get("lithology", {}).get("rock_zone_cleanup", {})
        if _cu.get("enabled", False) and _cu.get("column_top6_cleanup", False):
            _rock_zone = (gap_mask == 5)
            if _rock_zone.any():
                # Build group_id -> dominant_rock_block_idx LUT
                _bad_names = set(_cu.get("surface_bad_blocks", [])) | set(_cu.get("subsurface_bad_blocks", []))
                _bad_indices = frozenset(
                    pal._idx.get(n, -1) for n in _bad_names
                ) - {-1}
                if _bad_indices:
                    _groups_cfg = cfg.get("lithology", {}).get("groups", {})
                    _gid_to_rock_idx: dict[int, int] = {}
                    for _gn, _gd in _groups_cfg.items():
                        _gid_v = int(_gd.get("id", 0))
                        _palette = _gd.get("palette", ["stone"])
                        if _palette:
                            try:
                                _gid_to_rock_idx[_gid_v] = pal.idx(_palette[0])
                            except Exception:
                                _gid_to_rock_idx[_gid_v] = pal.idx("stone")
                    _stone_idx_fb = pal.idx("stone")
                    # Find rock pixels' (row, col, surface_y)
                    _rr, _cc = np.where(_rock_zone)
                    # S100 perf: vectorised (was a per-pixel × per-Y scalar
                    # loop).  Every (pixel, y_off) pair targets a distinct
                    # (y,row,col) cell, so batching per y_off is order-safe.
                    # gid→rock mapping replays dict.get exactly (unmapped or
                    # out-of-range ids fall back to stone); bad-block set
                    # membership becomes a boolean LUT over palette indices.
                    if lithology_tile.shape == _rock_zone.shape:
                        _gids = lithology_tile[_rr, _cc].astype(np.int64)
                    else:
                        _gids = np.zeros(len(_rr), dtype=np.int64)
                    _rock_for_px = np.full(len(_rr), _stone_idx_fb,
                                           dtype=np.uint16)
                    for _g, _ri in _gid_to_rock_idx.items():
                        _gm = _gids == _g
                        if _gm.any():
                            _rock_for_px[_gm] = _ri
                    _bad_lut = np.zeros(len(pal._names), dtype=bool)
                    _bad_lut[np.fromiter(_bad_indices, dtype=np.int64)] = True
                    _sy_px = surface_y[_rr, _cc].astype(np.int64)
                    # Check Y-2 down to Y-5 (4 blocks)
                    for _y_off in range(2, 6):
                        _y_rel = _sy_px - _y_off - Y_MIN
                        _okr = (_y_rel >= 0) & (_y_rel < Y_RANGE)
                        if not _okr.any():
                            continue
                        _yr = _y_rel[_okr]
                        _r2 = _rr[_okr]; _c2 = _cc[_okr]
                        _bad = _bad_lut[vol[_yr, _r2, _c2]]
                        if _bad.any():
                            vol[_yr[_bad], _r2[_bad], _c2[_bad]] = \
                                _rock_for_px[_okr][_bad]
                del _rock_zone

    # Re-assert the bedrock floor LAST so nothing overwrites it. vol[0] (Y_MIN,
    # the lowest world height) is set to bedrock at the top of this function, but
    # in the deepest ocean a column whose surface_y reaches Y_MIN has its
    # ocean-floor surface block written over vol[0]. Force bedrock back so every
    # column — land, coast, and abyssal ocean — has a guaranteed 1-block bedrock
    # layer at the world floor.
    vol[0, :, :] = pal.idx("bedrock")

    return vol, pal


# ---------------------------------------------------------------------------
# SCHEMATIC STAMPER
# ---------------------------------------------------------------------------
# S100 stamp-perf support.  stamp_schematic's per-block string path
# (str(cell) + pal.name_of() per visited cell — profiled 40.7M name_of
# calls / 61.7s over 9,796 stamps on a dense tile) now runs in the integer
# domain:
#   * _schem_stamp_data factors a schematic ONCE per file+rotation into a
#     unique-name list + int32 code grid, cached on the SchemData instance
#     (schematic_loader.load_schem caches per path per process);
#   * _pal_stamp_luts keeps incremental boolean classification LUTs over
#     palette indices ON the BlockPalette (protected-overwrite keys, soft
#     soil, log->wood eligibility, fence candidates) so per-cell substring
#     tests become one fancy-index.
# Byte-identical to the legacy scalar path — gated by
# tools/diag_nbt_emit_equiv.py stamp cases.

# Exact key tuple from the legacy per-cell overwrite check.
_STAMP_PROTECT_KEYS = ("log", "leaves", "fence", "planks",
                       "stairs", "slab", "vine")

# Hoisted verbatim from the legacy root-anchor loop's _SOFT_SOIL_NAMES.
_STAMP_SOFT_SOIL = frozenset({
    "dirt", "grass_block", "podzol", "coarse_dirt",
    "rooted_dirt", "mud", "packed_mud", "moss_block",
    "mycelium", "mud_block", "snow_block", "sand",
    "red_sand",
})

# Rotate directional blockstate properties to match spatial rotation.
# rot=1 (90° CW): north→east→south→west→north
# rot=2 (180°): north↔south, east↔west
# rot=3 (270° CW): north→west→south→east→north
_STAMP_DIR_REMAP = {
    1: {"north": "east", "east": "south", "south": "west", "west": "north"},
    2: {"north": "south", "south": "north", "east": "west", "west": "east"},
    3: {"north": "west", "west": "south", "south": "east", "east": "north"},
}


def _rotate_blockstate_name(name: str, remap: dict) -> str:
    """Exact clone of the legacy in-function _rotate_blockstate."""
    if "[" not in name:
        return name
    base, state = name.split("[", 1)
    state = state.rstrip("]")
    pairs = []
    for pair in state.split(","):
        k, sep, v = pair.partition("=")
        k = k.strip()
        v = v.strip()
        # Rotate directional keys (fence connections: north=true)
        if k in remap:
            k = remap[k]
        # Rotate facing values (gate/stair: facing=north)
        if k == "facing" and v in remap:
            v = remap[v]
        pairs.append(f"{k}={v}")
    return f"{base}[{','.join(pairs)}]"


def _pal_stamp_luts(pal: BlockPalette) -> dict:
    """Incremental per-palette boolean classification LUTs for the stamp
    path, stored on the BlockPalette instance.  The palette only APPENDS,
    so previously-classified prefixes stay valid; each call classifies only
    names added since the last call.  Each LUT replays the corresponding
    legacy per-name substring test exactly."""
    luts = pal._stamp_luts
    if luts is None:
        luts = {"n": 0,
                "protected": np.zeros(0, dtype=bool),
                "soil":      np.zeros(0, dtype=bool),
                "logswap":   np.zeros(0, dtype=bool),
                "fence":     np.zeros(0, dtype=bool)}
        pal._stamp_luts = luts
    n = len(pal._names)
    if n > luts["n"]:
        new = pal._names[luts["n"]:n]
        prot, soil, logsw, fence = [], [], [], []
        for nm in new:
            prot.append(any(k in nm for k in _STAMP_PROTECT_KEYS))
            bare = nm.replace("minecraft:", "").split("[")[0]
            soil.append(bare in _STAMP_SOFT_SOIL)
            # log->wood eligibility: "_log" in name, and horizontal
            # (axis=x / axis=z) logs skipped — same tests as legacy body.
            ok = "_log" in nm
            if ok and "[" in nm:
                props = nm.split("[", 1)[1].rstrip("]")
                if "axis=x" in props or "axis=z" in props:
                    ok = False
            logsw.append(ok)
            fence.append(("fence" in nm) and ("fence_gate" not in nm))
        luts["protected"] = np.concatenate(
            [luts["protected"], np.array(prot, dtype=bool)])
        luts["soil"] = np.concatenate(
            [luts["soil"], np.array(soil, dtype=bool)])
        luts["logswap"] = np.concatenate(
            [luts["logswap"], np.array(logsw, dtype=bool)])
        luts["fence"] = np.concatenate(
            [luts["fence"], np.array(fence, dtype=bool)])
        luts["n"] = n
    return luts


def _schem_stamp_data(schem_data, rot: int):
    """Factored stamp data for one SchemData + rotation, cached on the
    instance (one np.unique per schematic FILE per process instead of one
    per placement; one rot90+rename per rotation actually used).

    Returns (u_strs, codes, counts, u_air, u_log, u_bare):
      u_strs — final block-name string per code (post rotation-rename)
      codes  — int32 (Y,Z,X) grid of indices into u_strs (rotated)
      counts — cells per code (rotation-invariant)
      u_air  — "air" in name (substring, matching legacy behaviour)
      u_log  — "log" in name or "wood" in name
      u_bare — name with every "minecraft:" occurrence stripped

    Rotation renames operate on the NAME LIST, never on the object array;
    if a rename collides with another unique the two codes simply share a
    name — identical downstream behaviour to the legacy in-array rename."""
    blocks = schem_data.blocks
    cache = getattr(schem_data, "_stamp_cache", None)
    if cache is None or cache.get("blocks_id") != id(blocks):
        u_vals, inv = np.unique(blocks, return_inverse=True)
        base_strs = [str(u) for u in u_vals]
        base_codes = inv.reshape(blocks.shape).astype(np.int32)
        counts = np.bincount(base_codes.ravel(), minlength=len(base_strs))
        cache = {"blocks_id": id(blocks),
                 "base": (base_strs, base_codes, counts),
                 "rot": {}}
        try:
            schem_data._stamp_cache = cache
        except Exception:
            pass   # exotic schem_data object — just recompute per call
    # Legacy: rotation applied only when rot > 0; np.rot90 reduces k mod 4
    # and _DIR_REMAP.get(rot % 4) is None for k==0 — so rot%4==0 == base.
    key = (rot % 4) if rot > 0 else 0
    ent = cache["rot"].get(key)
    if ent is None:
        base_strs, base_codes, counts = cache["base"]
        if key == 0:
            u_strs, codes = base_strs, base_codes
        else:
            codes = np.rot90(base_codes, k=key, axes=(1, 2)).copy()
            remap = _STAMP_DIR_REMAP[key]
            u_strs = [_rotate_blockstate_name(s, remap) if "[" in s else s
                      for s in base_strs]
        nU = len(u_strs)
        u_air = np.fromiter(("air" in s for s in u_strs),
                            dtype=bool, count=nU)
        u_log = np.fromiter((("log" in s) or ("wood" in s) for s in u_strs),
                            dtype=bool, count=nU)
        u_bare = [s.replace("minecraft:", "") for s in u_strs]
        ent = (u_strs, codes, counts, u_air, u_log, u_bare)
        cache["rot"][key] = ent
    return ent


def stamp_schematic(
    vol:        np.ndarray,   # (Y_RANGE, H, W) uint16 �� modified in-place
    pal:        BlockPalette, # palette for index ↔ name conversion
    schem_data,               # SchemData dataclass or legacy dict
    local_x:    int,          # tile-local column (0..W-1)
    local_z:    int,          # tile-local row    (0..H-1)
    place_y:    int,          # world MC Y of schematic origin
    surface_y:  np.ndarray | None = None,  # (H, W) int16 — terrain surface
    water_col_mask: np.ndarray | None = None,  # (H, W) bool — S71: water columns (river+ocean)
    clip_oob:   bool = True,   # S95 tree-seam STEP A: OOB columns are the NEIGHBOUR's
                               # territory (culled/clipped), NOT "underwater" -> don't
                               # whole-reject a tree just for extending past the tile edge.
    deterministic_seat: bool = False,  # S100 seam-band: place_y is world-deterministic and
                               # the anchor may be OOB — SKIP the whole-stamp sink/reject
                               # decisions (they see only in-bounds columns, so the two
                               # tiles stamping the same tree would seat/reject differently
                               # = mismatched halves).  Per-column gates still apply.
) -> None:
    """
    Stamp a schematic into the volume array.
    Accepts either:
      - SchemData dataclass (blocks = (Y,Z,X) ndarray of block name strings)
      - Legacy dict with blocks = list of (sx, sy, sz, color, block_name, props)
    Skips air blocks. Clips to tile bounds silently.
    S61: each column is per-placement gated (desink + float-kill). Blocks at
    world_y <= local_surf are skipped (no buried trunks/roots). Columns whose
    lowest post-desink schem block has a ≥2-block gap to ground are rejected
    entirely (no floating leaves).
    """
    tile_H = vol.shape[1]
    tile_W = vol.shape[2]

    if isinstance(schem_data, dict):
        # Legacy dict format (used by smoke test)
        for (sx, sy, sz, _color, block_name, _props) in schem_data.get("blocks", []):
            if "air" in block_name:
                continue
            bare = block_name.replace("minecraft:", "")
            world_y = place_y + sy
            tile_z  = local_z + sz
            tile_x  = local_x + sx
            yi      = world_y - Y_MIN
            if 0 <= yi < Y_RANGE and 0 <= tile_z < tile_H and 0 <= tile_x < tile_W:
                vol[yi, tile_z, tile_x] = pal.idx(bare)
    else:
        # SchemData dataclass — blocks is (Y, Z, X) object array.
        # S100 stamp-perf: rotation (XZ plane, axes 1=Z, 2=X) + blockstate
        # property rotation + the unique-name factoring all live in
        # _schem_stamp_data (cached per SchemData + rotation).  From here on
        # the schematic is an int32 `codes` grid + per-code name LUTs; the
        # object block array is never touched again.
        if hasattr(schem_data, '_rotation'):
            rot = schem_data._rotation
        else:
            rot = 0
        u_strs, codes, u_counts, u_air, u_log, u_bare = \
            _schem_stamp_data(schem_data, rot)
        nU = len(u_strs)
        sh, sl, sw = codes.shape   # height, length (Z), width (X)

        # ── S61-f5: TRUNK EXTENSION anchor + bush sink fallback ────────────
        # Before rejecting a floating placement, try to ANCHOR it by extending
        # its trunk log downward to ground+1. A pine floating 3 blocks gets 2-3
        # extra spruce_log blocks beneath until it touches the ground.
        #
        # Strategy split:
        #   A. Placements WITH trunk columns (≥3 consecutive logs in a column):
        #      - No sink adjustment. Stamp at authored place_y.
        #      - Desink hides below-ground logs.
        #      - Post-stamp extension pass fills trunk-column gaps from
        #        local_surf+1 up to the lowest VISIBLE log, using the schem's
        #        primary log type.
        #      - Reject only if worst-trunk gap > MAX_TRUNK_EXT (6 blocks) —
        #        longer extensions read as "telephone pole" and break immersion.
        #   B. Placements WITHOUT trunks (bushes, log-less decorations):
        #      - Keep the f4 sink strategy: compute max gap over ground cols
        #        (lowest_sy ≤ 2); reject if > 3, else sink by (max_gap - 1).
        #      - No extension (nothing to fill with).
        #
        # Desink (skip world_y ≤ local_surf) applies in both branches — ground
        # stays intact, no buried trunks.
        #
        # Falls back to no-op when surface_y is None (smoke tests etc.).
        GROUND_COL_Y         = 2   # lowest_sy ≤ this → column is a "ground col"
        PLACE_MAX_FLOAT_BUSH = 3   # bush: max gap before reject
        TRUNK_RUN_MIN        = 2   # S70-f5 reverted from 1 back to 2 — single-log columns getting flagged as trunks caused branch-extension artifacts on bigger trees.  Original behavior: ≥2 consecutive logs = trunk.
        TRUNK_FIRST_MAX_Y    = 3   # lowest log sy must be ≤ this (else it's a branch)
        # S89 walk-3: trees on slopes float because the anchor sits uphill of the
        # trunk's own column. The fix is to SINK the whole tree (like bushes) so
        # the lowest trunk meets the real ground and the CANOPY drops with it --
        # NOT to extend a long pole up to the high anchor (that pushed crowns sky-
        # high, worse on tall trees). MAX_TRUNK_EXT is now just the small residual
        # fill after the sink; MAX_TREE_SINK is how far we'll seat before reject.
        MAX_TRUNK_EXT        = 8   # residual downhill fill after sink
        MAX_TREE_SINK        = 16  # max gap we'll seat by sinking; reject beyond
        if surface_y is not None:
            # Precompute non-air + log masks from the per-code LUTs.
            non_air = ~u_air[codes]
            is_log  = u_log[codes]
            # Count logs by BARE name (no blockstate) so axis=x/y/z variants
            # of the same species aggregate → primary picks correct species.
            # Iterated in sorted final-name order — matches the legacy
            # np.unique(blk_arr) walk including its dict insertion order
            # (max() tie-breaks on first-inserted).
            log_bare_counts: dict = {}
            for _k in sorted(range(nU), key=u_strs.__getitem__):
                su = u_strs[_k]
                if "air" in su:
                    continue
                if "log" in su or "wood" in su:
                    bare = su.replace("minecraft:", "").split("[")[0]
                    log_bare_counts[bare] = (
                        log_bare_counts.get(bare, 0) + int(u_counts[_k])
                    )
            any_col           = non_air.any(axis=0)    # (sl, sw) bool
            lowest_sy_col     = non_air.argmax(axis=0) # (sl, sw) int
            log_any_col       = is_log.any(axis=0)
            lowest_log_sy_col = is_log.argmax(axis=0)
            # Identify trunk columns: lowest log near ground (sy ≤ TRUNK_FIRST_MAX_Y)
            # AND consecutive log run from that position.
            # S70-f5: relative-run threshold — column qualifies only if its
            # run is ≥ max(TRUNK_RUN_MIN, max_run * TRUNK_RUN_FRAC).  This
            # filters branch logs that have shorter consecutive runs than
            # the main trunk (e.g., dosav_tree_soak_b_sm has main trunk
            # run=6 but branch columns at run=2..3 were qualifying as
            # "trunks", causing trunk-extension to fill fake side-trunks
            # under leaf clusters → bushy "no visible trunk" look).
            TRUNK_RUN_FRAC = 0.85   # S70-f5b: bumped from 0.6 — adjacent biome (DESERT_STEPPE_TRANSITION) still showed bushy fake trunks at 0.6
            # S100 stamp-perf: consecutive-log run length from the lowest
            # log, vectorised (was a per-column python scan).  run = index
            # of the first non-log at/after `first`, minus `first`; no
            # break → runs to the top (sh - first).
            runs = np.zeros((sl, sw), dtype=np.int32)
            _elig = log_any_col & (lowest_log_sy_col <= TRUNK_FIRST_MAX_Y)
            if _elig.any():
                _yy3 = np.arange(sh)[:, None, None]
                _brk = (~is_log) & (_yy3 >= lowest_log_sy_col[None, :, :])
                _has_brk = _brk.any(axis=0)
                _first_brk = _brk.argmax(axis=0)
                _run_all = np.where(_has_brk,
                                    _first_brk - lowest_log_sy_col,
                                    sh - lowest_log_sy_col)
                runs[_elig] = _run_all[_elig].astype(np.int32)
            max_run = int(runs.max()) if runs.size else 0
            trunk_threshold = max(TRUNK_RUN_MIN, int(max_run * TRUNK_RUN_FRAC))
            is_trunk_col = (runs >= trunk_threshold) & (runs > 0)
            has_trunks = bool(is_trunk_col.any())
            # Bbox out-of-tile reject + desink cutoff per column (S100:
            # vectorised — same flags; the legacy below-sea/water elif both
            # set the same reject bit, so the union is identical).
            _tz_arr = local_z + np.arange(sl)
            _tx_arr = local_x + np.arange(sw)
            _z_in = (_tz_arr >= 0) & (_tz_arr < tile_H)
            _x_in = (_tx_arr >= 0) & (_tx_arr < tile_W)
            _inb = _z_in[:, None] & _x_in[None, :]
            col_oob = ~_inb                    # S95 STEP A: outside tile
            col_reject = col_oob.copy()
            col_desink = np.full((sl, sw), -(1 << 30), dtype=np.int64)
            _wm_grid = None
            if _inb.any():
                _ix = np.ix_(np.where(_z_in)[0], np.where(_x_in)[0])
                _tzv = _tz_arr[_z_in][:, None]
                _txv = _tx_arr[_x_in][None, :]
                col_desink[_ix] = surface_y[_tzv, _txv].astype(np.int64)
                # S64: footprint water gate — reject any column whose
                # surface is below sea level so trees never leave leaves
                # hanging over water from multi-column canopies.
                # S71: extended to also reject river-water columns (carved
                # rivers above sea level) via water_col_mask.  This catches
                # trees with canopy hanging over a river even though the
                # placement-time 14px buffer should prevent it — defence
                # in depth.  Whole-stamp reject below trips on this too.
                col_reject |= _inb & (col_desink < 63)
                if water_col_mask is not None:
                    _wm_grid = np.zeros((sl, sw), dtype=bool)
                    _wm_grid[_ix] = water_col_mask[_tzv, _txv]
                    col_reject |= _wm_grid
            # S65: WHOLE-SCHEMATIC REJECT if ANY footprint column would
            # stand underwater.  Previous S64 fix only skipped underwater
            # columns, but land-side columns still left leaf clusters
            # overhanging toward water.  Abort the entire stamp instead.
            # (Only applies when the column has any content — an all-air
            # column is harmless even underwater.)
            # S70-f5: reverted f4 relaxation — the canopy-anchor pass made
            # branch leaves WORSE by adding fake logs under them.  User
            # wants floating leaves above ground (natural look), not
            # synthesized trunks under every leaf cluster.
            # Below-sea reject (oceans, deep lakes). S95 tree-seam STEP A:
            # an OOB column has the sentinel col_desink (-big) and is the
            # NEIGHBOUR's territory (clipped, not this tile's water) -> it must
            # NOT trigger the whole-stamp underwater reject, else every tree
            # touching the far tile edge is dropped = the seam trench. A truly
            # in-bounds underwater column still whole-rejects (S64/S65/S71 intact).
            # S71: river-water reject (carved channels above sea level) via
            # _wm_grid (in-bounds columns only, matching the legacy bounds
            # check).  S100: vectorised — legacy early-break scan == any().
            _cand = col_reject & any_col
            _a_hit = _cand & (col_desink < 63)
            if clip_oob:
                _a_hit &= ~col_oob
            _uwater_hit = bool(_a_hit.any())
            if (not _uwater_hit) and _wm_grid is not None:
                _uwater_hit = bool((_cand & _wm_grid).any())
            if _uwater_hit and not deterministic_seat:
                # S100: band trees skip the whole-stamp reject (asymmetric across
                # tiles); their per-column col_reject still skips water columns,
                # and the placement-side symmetric footprint gate pre-filters.
                return
            if has_trunks:
                # Strategy A — SINK then small extension. Compute worst trunk gap
                # (how far the lowest trunk floats above its own column ground).
                # S70-f5: reverted f4 — back to original (skip col_reject).
                _tcols = is_trunk_col & ~col_reject
                if _tcols.any():
                    _tgaps = ((place_y + lowest_log_sy_col.astype(np.int64))
                              - col_desink)
                    max_trunk_gap = max(0, int(_tgaps[_tcols].max()))
                else:
                    max_trunk_gap = 0
                if (not deterministic_seat) and max_trunk_gap > MAX_TREE_SINK:
                    return  # slope too steep to seat the tree cleanly — reject
                # S89 walk-3: SINK the whole schematic so the lowest trunk drops to
                # ~1 above ground and the CANOPY comes down with it (no flagpole).
                # Uphill blocks that now fall below their column ground are hidden
                # by the per-column underground cull in the stamp loop. A small
                # residual gap is closed by the trunk extension fill below.
                # S100: band trees keep the authored world-deterministic place_y
                # (the gap is computed over in-bounds columns only → the two
                # tiles would sink the same tree by different amounts).
                if (not deterministic_seat) and max_trunk_gap > 1:
                    place_y -= (max_trunk_gap - 1)
                # Pick primary log type for canopy-only-column fallback.
                # Bare name (no axis) → MC defaults to axis=y (vertical).
                if log_bare_counts:
                    primary_log_bare = max(log_bare_counts, key=log_bare_counts.get)
                    primary_log_idx = pal.idx(primary_log_bare)
                else:
                    primary_log_idx = None
            else:
                # Strategy B — bush sink.  S70: real fix for "columns of
                # leaves" bug moved to TRUNK_RUN_MIN=1 above (catches
                # single-log small trees as trees instead of bushes).
                # This bush-sink path now only runs for schematics with
                # genuinely zero log blocks anywhere — true leafy bushes.
                _gcols = ((~col_reject) & any_col
                          & (lowest_sy_col <= GROUND_COL_Y))
                any_ground_col = bool(_gcols.any())
                if any_ground_col:
                    _bgaps = ((place_y + lowest_sy_col.astype(np.int64))
                              - col_desink)
                    max_gap = max(0, int(_bgaps[_gcols].max()))
                else:
                    max_gap = 0
                if (not deterministic_seat) and any_ground_col and max_gap > PLACE_MAX_FLOAT_BUSH:
                    return
                if (not deterministic_seat) and max_gap > 1:
                    place_y -= (max_gap - 1)
                primary_log_idx = None
                # S74 canopy-reject CANCELED in S75: user reviewed the 26-
                # biome render (which was made WITHOUT this reject in effect)
                # and found bush placement looked great.  Keep the existing
                # PLACE_MAX_FLOAT_BUSH ground-col gating as the only bush
                # gate.  Trees still use MAX_TRUNK_EXT=6 (unchanged).
        else:
            col_reject = None
            col_desink = None
            has_trunks = False
            is_trunk_col = None
            is_log = None
            non_air = None
            primary_log_idx = None

        # ── main stamp (S100: vectorised; boolean masking follows C order,
        # identical to the legacy sy→sz→sx visit order) ─────────────────────
        _sy_lo = max(0, Y_MIN - place_y)
        _sy_hi = min(sh, Y_RANGE + Y_MIN - place_y)
        _sz_lo = max(0, -local_z);  _sz_hi = min(sl, tile_H - local_z)
        _sx_lo = max(0, -local_x);  _sx_hi = min(sw, tile_W - local_x)
        if _sy_hi > _sy_lo and _sz_hi > _sz_lo and _sx_hi > _sx_lo:
            _codes_c = codes[_sy_lo:_sy_hi, _sz_lo:_sz_hi, _sx_lo:_sx_hi]
            _wmask = ~u_air[_codes_c]
            # S61 column-level gates
            if col_reject is not None:
                _wmask &= ~col_reject[_sz_lo:_sz_hi, _sx_lo:_sx_hi][None, :, :]
                _wys = place_y + np.arange(_sy_lo, _sy_hi, dtype=np.int64)
                _wmask &= (_wys[:, None, None]
                           > col_desink[_sz_lo:_sz_hi, _sx_lo:_sx_hi][None, :, :])
            if _wmask.any():
                _dest = vol[place_y + _sy_lo - Y_MIN:place_y + _sy_hi - Y_MIN,
                            local_z + _sz_lo:local_z + _sz_hi,
                            local_x + _sx_lo:local_x + _sx_hi]
                # Don't overwrite existing schematic blocks (legacy per-cell
                # name_of + substring test → palette-index LUT; _dest is a
                # view — existing values all predate this placement's writes)
                _wmask &= ~_pal_stamp_luts(pal)["protected"][_dest]
                if _wmask.any():
                    _sel = _codes_c[_wmask]   # C order == legacy visit order
                    # pal.idx in first-encounter order → palette growth
                    # order identical to the per-cell scalar loop
                    _uq, _fi = np.unique(_sel, return_index=True)
                    _dest_lut = np.zeros(nU, dtype=np.uint16)
                    for _k in _uq[np.argsort(_fi, kind="stable")]:
                        _dest_lut[_k] = pal.idx(u_bare[_k])
                    _dest[_wmask] = _dest_lut[_sel]

        # ── S61-f6: TRUNK EXTENSION pass (post-stamp, per-column log type) ──
        # For each trunk column, fill any gap between local_surf+1 and the
        # lowest VISIBLE schem log. Each column uses ITS OWN lowest-visible log
        # type (stripped of axis/state props → defaults to axis=y = vertical).
        # Multi-thick trunks with same wood extend consistently; mixed-bark
        # trunks (e.g. branch log different from trunk log) extend with the
        # column-local wood.
        #
        # Fallback: if the entire trunk column got desinked (all logs sunk
        # below local_surf), extend up to the lowest visible canopy content
        # anyway — prevents a floating leaf mass with no supporting trunk
        # beneath it on steep uphill columns. Fallback uses primary_log_idx
        # (the most-common log bare name across the placement).
        if (surface_y is not None and has_trunks
                and primary_log_idx is not None and non_air is not None):
            AIR_IDX_EXT = pal.air
            _col_log_idx_cache: dict = {}
            # S100 stamp-perf: root-anchor soil test via palette LUT (was
            # pal.name_of + split per probed cell).  Snapshot is safe: the
            # cells this pass READS (at/below each trunk column's own
            # surface) are disjoint from the cells it WRITES (other columns
            # / above-surface fills), so no read ever sees an index newer
            # than the snapshot; the length guard replays name_of()'s
            # out-of-range -> "" -> not-soil behaviour.
            _soil_lut = _pal_stamp_luts(pal)["soil"]
            _soil_n = len(_soil_lut)
            for _sz in range(sl):
                if not is_trunk_col[_sz].any():
                    continue
                tile_z = local_z + _sz
                if not (0 <= tile_z < tile_H):
                    continue
                for _sx in range(sw):
                    if not is_trunk_col[_sz, _sx] or col_reject[_sz, _sx]:
                        continue
                    tile_x = local_x + _sx
                    if not (0 <= tile_x < tile_W):
                        continue
                    ls = int(col_desink[_sz, _sx])
                    # Lowest VISIBLE log sy (first log with world_y > ls)
                    target_sy = -1
                    fill_idx = None
                    for _sy in range(sh):
                        if not is_log[_sy, _sz, _sx]:
                            continue
                        if place_y + _sy > ls:
                            target_sy = _sy
                            # Per-column: take THIS column's log type, bare
                            col_log_name = u_strs[codes[_sy, _sz, _sx]]
                            bare = col_log_name.replace("minecraft:", "").split("[")[0]
                            fill_idx = _col_log_idx_cache.get(bare)
                            if fill_idx is None:
                                fill_idx = pal.idx(bare)
                                _col_log_idx_cache[bare] = fill_idx
                            break
                    if target_sy < 0:
                        # All logs in this column sunk → fall back to canopy
                        # content target, primary log type for fill.
                        fill_idx = primary_log_idx
                        for _sy in range(sh):
                            if not non_air[_sy, _sz, _sx]:
                                continue
                            if place_y + _sy > ls + 1:
                                target_sy = _sy
                                break
                    # S66 (fixed): ROOT ANCHOR runs for EVERY trunk column,
                    # regardless of whether an UP-extension was needed.  User
                    # wants roots on all sloped trees.  Previous version was
                    # inside the `target_wy <= ls + 1: continue` branch so
                    # flat-terrain columns never got roots.
                    ROOT_ANCHOR_DEPTH = 6
                    # (soft-soil set hoisted to module _STAMP_SOFT_SOIL;
                    # the test itself is the _soil_lut palette LUT above)
                    for drop in range(1, ROOT_ANCHOR_DEPTH + 1):
                        fill_wy_down = ls - drop + 1  # ls is surface Y, so fill ls..ls-5
                        yi_down = fill_wy_down - Y_MIN
                        if not (0 <= yi_down < Y_RANGE):
                            continue
                        existing = vol[yi_down, tile_z, tile_x]
                        if existing < _soil_n and _soil_lut[existing]:
                            vol[yi_down, tile_z, tile_x] = fill_idx

                    # Only run the UP-extension fill if needed
                    if target_sy < 0 or fill_idx is None:
                        continue
                    target_wy = place_y + target_sy
                    if target_wy <= ls + 1:
                        continue  # already touching — no extension UP needed
                    ext_span = target_wy - (ls + 1)
                    if ext_span > MAX_TRUNK_EXT:
                        continue
                    for fill_wy in range(ls + 1, target_wy):
                        yi = fill_wy - Y_MIN
                        if not (0 <= yi < Y_RANGE):
                            continue
                        if vol[yi, tile_z, tile_x] == AIR_IDX_EXT:
                            vol[yi, tile_z, tile_x] = fill_idx

        # ── S62-stretch: LOG → WOOD end-grain swap ─────────────────────────
        # `_log` blocks show cut end-grain on top/bottom faces; `_wood` shows
        # bark on all 6 faces.  Swap `_log` → `_wood` in any stamped footprint
        # cell where the adjacent block directly above OR below is air.  This
        # catches: exposed tops of trunks without full canopy cover, floating
        # end caps of thick trunks, open shelf logs.  Leaves-above does NOT
        # trigger (canopy hides the end-grain visually).  Horizontal logs
        # (axis=x/z) are skipped — their end grain already faces the trunk or
        # canopy and swapping loses the cut-wood texture where it reads as
        # a broken branch.
        air_idx_swap = pal.air
        _wood_idx_cache: dict = {}
        for sy in range(sh):
            world_y = place_y + sy
            yi = world_y - Y_MIN
            if yi <= 0 or yi >= Y_RANGE - 1:
                continue
            for sz in range(sl):
                tile_z = local_z + sz
                if not (0 <= tile_z < tile_H):
                    continue
                for sx in range(sw):
                    tile_x = local_x + sx
                    if not (0 <= tile_x < tile_W):
                        continue
                    cur_idx = vol[yi, tile_z, tile_x]
                    cur_name = pal.name_of(cur_idx)
                    if "_log" not in cur_name:
                        continue
                    # Skip horizontal logs (axis=x or axis=z).  axis=y or no
                    # property → vertical, eligible.
                    if "[" in cur_name:
                        props = cur_name.split("[", 1)[1].rstrip("]")
                        if "axis=x" in props or "axis=z" in props:
                            continue
                    # Need either air directly above or directly below.
                    if (vol[yi + 1, tile_z, tile_x] != air_idx_swap
                            and vol[yi - 1, tile_z, tile_x] != air_idx_swap):
                        continue
                    bare = cur_name.replace("minecraft:", "").split("[")[0]
                    wood_bare = bare.replace("_log", "_wood")
                    if wood_bare == bare:
                        continue
                    w_idx = _wood_idx_cache.get(wood_bare)
                    if w_idx is None:
                        try:
                            w_idx = pal.idx(wood_bare)
                        except Exception:
                            _wood_idx_cache[wood_bare] = -1
                            continue
                        _wood_idx_cache[wood_bare] = w_idx
                    elif w_idx == -1:
                        continue
                    vol[yi, tile_z, tile_x] = w_idx

        # ── S65: FENCE CONNECTION PROPERTY PASS ─────────────────────────
        # MC fences default to all-disconnected when placed via worldgen (no
        # neighbor update triggers).  Scan every fence block in the just-
        # stamped footprint and set north/south/east/west based on the
        # connective block at that face.
        _FENCE_SUFFIXES = ("_fence", "_fence_gate")  # both work the same
        _CONNECTS = ("_fence", "_fence_gate", "_log", "_wood", "_planks",
                      "_wall", "_stairs")
        _SOLID_CONNECT = frozenset({
            "stone", "cobblestone", "andesite", "granite", "diorite",
            "deepslate", "tuff", "sandstone", "dirt", "grass_block",
            "podzol", "moss_block",
        })
        def _fence_connects_to(name: str) -> bool:
            if not name or name == "air":
                return False
            if any(suf in name for suf in _CONNECTS):
                return True
            return name in _SOLID_CONNECT
        _fence_cache: dict = {}
        for sy in range(sh):
            world_y = place_y + sy
            yi = world_y - Y_MIN
            if yi < 0 or yi >= Y_RANGE:
                continue
            for sz in range(sl):
                tile_z = local_z + sz
                if not (0 <= tile_z < tile_H):
                    continue
                for sx in range(sw):
                    tile_x = local_x + sx
                    if not (0 <= tile_x < tile_W):
                        continue
                    idx_here = vol[yi, tile_z, tile_x]
                    name_here = pal.name_of(idx_here)
                    if "fence" not in name_here or "fence_gate" in name_here:
                        continue
                    # Parse bare + existing properties
                    if "[" in name_here:
                        bare_part, _, props_part = name_here.partition("[")
                        existing = props_part.rstrip("]")
                    else:
                        bare_part = name_here
                        existing = ""
                    bare = bare_part.replace("minecraft:", "")
                    # Compute neighbour connections
                    def _neighbor_name(dz, dx):
                        tz2, tx2 = tile_z + dz, tile_x + dx
                        if not (0 <= tz2 < tile_H and 0 <= tx2 < tile_W):
                            return "air"
                        return pal.name_of(vol[yi, tz2, tx2])
                    n_conn = _fence_connects_to(_neighbor_name(-1, 0))
                    s_conn = _fence_connects_to(_neighbor_name(+1, 0))
                    e_conn = _fence_connects_to(_neighbor_name(0, +1))
                    w_conn = _fence_connects_to(_neighbor_name(0, -1))
                    # Preserve non-connection properties
                    kept = []
                    if existing:
                        for kv in existing.split(","):
                            k, _, v = kv.partition("=")
                            if k.strip() in ("north", "south", "east", "west"):
                                continue
                            if k and v:
                                kept.append(f"{k.strip()}={v.strip()}")
                    kept.extend([
                        f"north={'true' if n_conn else 'false'}",
                        f"south={'true' if s_conn else 'false'}",
                        f"east={'true' if e_conn else 'false'}",
                        f"west={'true' if w_conn else 'false'}",
                    ])
                    new_name = f"{bare}[{','.join(kept)}]"
                    new_idx = _fence_cache.get(new_name)
                    if new_idx is None:
                        try:
                            new_idx = pal.idx(new_name)
                        except Exception:
                            _fence_cache[new_name] = -1
                            continue
                        _fence_cache[new_name] = new_idx
                    elif new_idx == -1:
                        continue
                    vol[yi, tile_z, tile_x] = new_idx


# ---------------------------------------------------------------------------
# 1.20.1 DIRECT REGION WRITER  (nbtlib + raw Anvil .mca — no amulet)
# ---------------------------------------------------------------------------
# Each 512×512-block tile maps exactly to one .mca region file.
# DataVersion 4556 = Java 1.21.10.  S84: 48 sections cover Y=-64 to Y=703 (was 32 sections / Y 447 for 448-height world).
# Block states and biomes use the 1.18+ padded long-array format.
# ---------------------------------------------------------------------------

import math      as _math
import io        as _io
import zlib      as _zlib
import struct    as _struct
import traceback as _traceback

_CHUNK_DATA_VERSION = 4556   # Java 1.21.10
_SECTION_Y_MIN      = -4     # Y_MIN // 16 = -64 // 16
_N_SECTIONS         = 48     # S84: (704 - (-64)) // 16 = 48 (was 32 for 512-block world)
_SECTOR_SZ          = 4096   # Anvil .mca sector size

_TEST_SECTION_Y_MAX = None   # Full Higher Heights range: sections -4 to 43 (Y -64 to 703)

# S84: skip-empty-sections fast path. Most sections at high Y are entirely
# air. Build the trivial all-air block_states Compound once at module
# scope and reuse for every all-air section we emit. Avoids ~1-2ms of
# np.unique + _entry parsing + Compound construction per air section
# (= ~25,000-40,000 calls per tile on average → ~25-80s saved per tile).
# Biome compound stays per-section because biome can vary across sections
# (sky-biome override for BOREAL_ALPINE altitude snow, etc.).
_AIR_BLOCK_STATES_NBT = None  # lazy-built on first use (after nbtlib import)


def _get_air_block_states_nbt():
    """Return the cached all-air block_states Compound, building on
    first use. Same Compound instance is reused — nbtlib serialization
    reads but does not mutate."""
    global _AIR_BLOCK_STATES_NBT
    if _AIR_BLOCK_STATES_NBT is None:
        import nbtlib
        _AIR_BLOCK_STATES_NBT = nbtlib.Compound({
            "palette": nbtlib.List[nbtlib.Compound]([
                nbtlib.Compound({"Name": nbtlib.String("minecraft:air")})
            ])
        })
    return _AIR_BLOCK_STATES_NBT


def _pack_indices(indices: np.ndarray, palette_size: int, min_bits: int = 4) -> np.ndarray:
    """Pack palette indices into 64-bit longs using 1.18+ padded format.
    min_bits=4 for block_states, min_bits=1 for biomes."""
    bpe = max(min_bits, _math.ceil(_math.log2(max(palette_size, 2))))
    vpl = 64 // bpe
    n   = len(indices)
    n_longs = _math.ceil(n / vpl)
    pad     = n_longs * vpl - n
    padded      = np.concatenate([indices.astype(np.int64), np.zeros(pad, dtype=np.int64)])
    reshaped    = padded.reshape(n_longs, vpl)
    bit_offsets = (np.arange(vpl, dtype=np.int64) * bpe).reshape(1, vpl)
    return np.bitwise_or.reduce(reshaped << bit_offsets, axis=1)


def _build_block_states_nbt(names_yzx: np.ndarray):
    """Build block_states Compound from (16,16,16) block name array (Y,Z,X)."""
    import nbtlib
    flat          = names_yzx.ravel()
    unique, inv   = np.unique(flat, return_inverse=True)

    def _entry(name: str):
        # Parse optional block state: "tall_grass[half=upper]" → name + properties
        props_dict = {}
        if "[" in name:
            base, _, state_str = name.partition("[")
            state_str = state_str.rstrip("]")
            for pair in state_str.split(","):
                k, _, v = pair.partition("=")
                if k and v:
                    props_dict[k.strip()] = nbtlib.String(v.strip())
            name = base
        ns, _, bare = name.rpartition(":")
        full_name = f"{'minecraft' if not ns else ns}:{bare}"
        # Prevent leaf decay on schematic-placed leaves
        if "leaves" in bare and "persistent" not in props_dict:
            props_dict["persistent"] = nbtlib.String("true")
            props_dict.setdefault("distance", nbtlib.String("1"))
        # S64: ensure waterlogged=true for sea_pickle placed in ocean water
        # (seagrass/kelp are inherently water-containing, no property needed)
        if bare == "sea_pickle" and "waterlogged" not in props_dict:
            props_dict["waterlogged"] = nbtlib.String("true")
        entry = {"Name": nbtlib.String(full_name)}
        if props_dict:
            entry["Properties"] = nbtlib.Compound(props_dict)
        return nbtlib.Compound(entry)

    palette = nbtlib.List[nbtlib.Compound]([_entry(n) for n in unique])
    if len(unique) == 1:
        return nbtlib.Compound({"palette": palette})
    longs = _pack_indices(inv, len(unique))
    return nbtlib.Compound({"palette": palette,
                             "data":    nbtlib.LongArray(longs.tolist())})


def _build_biomes_nbt(names_yzx: np.ndarray):
    """Build biomes Compound from (4,4,4) MC biome-string array (Y,Z,X quanta)."""
    import nbtlib
    flat         = names_yzx.ravel()
    unique, inv  = np.unique(flat, return_inverse=True)
    palette      = nbtlib.List[nbtlib.String]([nbtlib.String(n) for n in unique])
    if len(unique) == 1:
        return nbtlib.Compound({"palette": palette})
    longs = _pack_indices(inv, len(unique), min_bits=1)  # biomes: no 4-bit floor
    return nbtlib.Compound({"palette": palette,
                             "data":    nbtlib.LongArray(longs.tolist())})


_FLUID_NAMES = frozenset({"water", "lava", "minecraft:water", "minecraft:lava"})

def _build_heightmaps_nbt(chunk_vol: np.ndarray) -> "nbtlib.Compound":
    """
    Build MC heightmaps from (Y_RANGE, 16, 16) block array.

    Encoding: 9-bit padded longs, 37 longs for 256 columns.
    stored_value = volume Y-index of highest matching block
                 = MC_Y - Y_MIN  (e.g. sea-level Y=63 → stored=127 for Y_MIN=-64)

    Returns Compound with WORLD_SURFACE, OCEAN_FLOOR,
    MOTION_BLOCKING, MOTION_BLOCKING_NO_LEAVES.
    """
    import nbtlib
    CHUNK_SZ = 16
    flat = chunk_vol.reshape(Y_RANGE, CHUNK_SZ * CHUNK_SZ)   # (Y_RANGE, 256)

    is_air   = (flat == "air")
    is_fluid = np.isin(flat, list(_FLUID_NAMES))

    def _highest_yi(solid_mask: np.ndarray) -> np.ndarray:
        """solid_mask: (Y_RANGE, 256) bool — return (256,) stored heightmap values."""
        flipped  = solid_mask[::-1, :]
        has_any  = solid_mask.any(axis=0)
        first_hi = np.argmax(flipped, axis=0)
        highest  = np.where(has_any, Y_RANGE - 1 - first_hi, 0)
        return highest.astype(np.int64)          # stored = yi = MC_Y - Y_MIN

    def _pack_hm(values: np.ndarray) -> "nbtlib.LongArray":
        # S84: bits_per_entry derived from world height. 9 bits maxes at 511
        # (was correct for 512-block world), but 768-block world needs 10
        # bits (max 1023). MC computes bits per heightmap as
        # ceil(log2(world_height + 1)) — matches our derivation.
        bpe     = _math.ceil(_math.log2(Y_RANGE + 1))
        vpl     = 64 // bpe                      # 7 values/long @ 9bpe; 6 @ 10bpe
        n_longs = _math.ceil(len(values) / vpl)  # 37 @ 9bpe; 43 @ 10bpe
        pad     = n_longs * vpl - len(values)
        padded  = np.concatenate([values, np.zeros(pad, dtype=np.int64)])
        reshaped    = padded.reshape(n_longs, vpl)
        bit_offsets = (np.arange(vpl, dtype=np.int64) * bpe).reshape(1, vpl)
        longs = np.bitwise_or.reduce(reshaped << bit_offsets, axis=1)
        return nbtlib.LongArray(longs.tolist())

    ws = _highest_yi(~is_air)                    # water counts as surface
    of = _highest_yi(~is_air & ~is_fluid)        # highest non-fluid solid

    return nbtlib.Compound({
        "WORLD_SURFACE":             _pack_hm(ws),
        "OCEAN_FLOOR":               _pack_hm(of),
        "MOTION_BLOCKING":           _pack_hm(ws),
        "MOTION_BLOCKING_NO_LEAVES": _pack_hm(of),  # no leaves in terrain yet
    })


# ---------------------------------------------------------------------------
# S100 PERF — index-domain NBT emit helpers.
#
# _chunk_to_nbt_bytes used to materialise the full (Y_RANGE,16,16) chunk as
# object-dtype STRINGS and run np.full / np.unique / np.isin over python
# string objects per section (profiled 50.3s cumulative on a dense tile:
# np.full 14s, per-section object-unique ~18s, heightmap np.isin 8.4s,
# to_strings 1.5s).  The volume is ALREADY uint16 palette indices, so all of
# that now runs in the integer domain:
#   * per-section np.unique over uint16 (radix-cheap) + a NAME-order re-sort
#     that reproduces EXACTLY the lexicographic palette order np.unique on
#     strings produced (palette order changes the emitted bytes);
#   * palette-entry Compounds memoized per block name (module scope — output
#     depends only on the name).  Compound instances are SHARED across
#     sections/chunks/tiles: nbtlib serialization reads but never mutates
#     (same contract as the S84 _AIR_BLOCK_STATES_NBT cache) and
#     List.cast_item passes existing Compound instances through untouched;
#   * biome-section Compounds memoized by their 64-cell value pattern
#     (uniform-biome tiles hit a handful of patterns);
#   * heightmap air/fluid tests become an integer compare + a boolean LUT
#     over palette indices.
# The original string-domain builders above are kept intact (reference
# implementations; islands/biome_tint_overlay.py imports _build_biomes_nbt).
# Byte-for-byte equivalence gated by tools/diag_nbt_emit_equiv.py.
# ---------------------------------------------------------------------------

_PALETTE_ENTRY_NBT_CACHE: dict = {}          # block name -> palette Compound
_SINGLE_BLOCK_STATES_NBT_CACHE: dict = {}    # block name -> 1-entry block_states
_BIOME_NBT_CACHE: dict = {}                  # 64-cell name tuple -> biomes Compound
_BIOME_NBT_CACHE_MAX = 8192                  # safety valve (entries are tiny)
_EMPTY_SEC_U16 = np.zeros((16, 16, 16), dtype=np.uint16)  # read-only all-air


def _palette_entry_nbt(name: str):
    """Cached block_states palette-entry Compound for one block-name string.

    Exact clone of _build_block_states_nbt's inner _entry parse (optional
    "[key=value,...]" properties, namespace default to minecraft:, leaf
    persistent/distance injection, sea_pickle waterlogged injection) —
    built once per distinct name instead of once per section."""
    ent = _PALETTE_ENTRY_NBT_CACHE.get(name)
    if ent is not None:
        return ent
    import nbtlib
    key = name
    props_dict = {}
    if "[" in name:
        base, _, state_str = name.partition("[")
        state_str = state_str.rstrip("]")
        for pair in state_str.split(","):
            k, _, v = pair.partition("=")
            if k and v:
                props_dict[k.strip()] = nbtlib.String(v.strip())
        name = base
    ns, _, bare = name.rpartition(":")
    full_name = f"{'minecraft' if not ns else ns}:{bare}"
    # Prevent leaf decay on schematic-placed leaves
    if "leaves" in bare and "persistent" not in props_dict:
        props_dict["persistent"] = nbtlib.String("true")
        props_dict.setdefault("distance", nbtlib.String("1"))
    # S64: ensure waterlogged=true for sea_pickle placed in ocean water
    if bare == "sea_pickle" and "waterlogged" not in props_dict:
        props_dict["waterlogged"] = nbtlib.String("true")
    entry = {"Name": nbtlib.String(full_name)}
    if props_dict:
        entry["Properties"] = nbtlib.Compound(props_dict)
    ent = nbtlib.Compound(entry)
    _PALETTE_ENTRY_NBT_CACHE[key] = ent
    return ent


def _build_block_states_nbt_u16(sec_u16: np.ndarray, pal: BlockPalette):
    """Index-domain equivalent of _build_block_states_nbt(pal.to_strings(s)).

    np.unique runs on the uint16 indices (~50x cheaper than an argsort over
    4096 python-string objects); the unique indices are then mapped to names
    and re-ordered BY NAME so the palette list and the packed data longs
    reproduce exactly the bytes of np.unique on the string array (which
    sorts lexicographically — object-dtype np.sort uses Python str `<`,
    same total order as sorted()).  Relies on the BlockPalette name<->index
    bijection (idx() dedups; every index maps to a distinct name)."""
    import nbtlib
    u_idx, inv = np.unique(sec_u16.ravel(), return_inverse=True)
    names = [pal._names[i] for i in u_idx]
    if len(names) == 1:
        name = names[0]
        cached = _SINGLE_BLOCK_STATES_NBT_CACHE.get(name)
        if cached is None:
            cached = nbtlib.Compound({
                "palette": nbtlib.List[nbtlib.Compound](
                    [_palette_entry_nbt(name)])
            })
            _SINGLE_BLOCK_STATES_NBT_CACHE[name] = cached
        return cached
    order = sorted(range(len(names)), key=names.__getitem__)
    rank = np.empty(len(order), dtype=np.int64)
    rank[np.asarray(order, dtype=np.int64)] = np.arange(len(order),
                                                        dtype=np.int64)
    sorted_names = [names[k] for k in order]
    palette = nbtlib.List[nbtlib.Compound](
        [_palette_entry_nbt(n) for n in sorted_names])
    longs = _pack_indices(rank[inv], len(sorted_names))
    return nbtlib.Compound({"palette": palette,
                             "data":    nbtlib.LongArray(longs.tolist())})


def _build_biomes_nbt_cached(names_yzx: np.ndarray):
    """Value-keyed cache over _build_biomes_nbt.

    Output depends only on the 64 cell strings, so the Compound is memoized
    on their tuple.  The SAME Compound instance is shared across sections /
    chunks (serialization-only use, see module comment above)."""
    key = tuple(names_yzx.ravel().tolist())
    hit = _BIOME_NBT_CACHE.get(key)
    if hit is None:
        if len(_BIOME_NBT_CACHE) >= _BIOME_NBT_CACHE_MAX:
            _BIOME_NBT_CACHE.clear()
        hit = _build_biomes_nbt(names_yzx)
        _BIOME_NBT_CACHE[key] = hit
    return hit


def _build_heightmaps_nbt_u16(chunk_u16: np.ndarray,
                              pal: BlockPalette) -> "nbtlib.Compound":
    """Index-domain equivalent of _build_heightmaps_nbt(pal.to_strings(v)).

    air == index 0 (BlockPalette invariant), and the fluid test is a boolean
    LUT over palette indices instead of np.isin over 196k object strings
    (profiled 8.4s/tile).  Packing math identical to _build_heightmaps_nbt."""
    import nbtlib
    CHUNK_SZ = 16
    flat = chunk_u16.reshape(Y_RANGE, CHUNK_SZ * CHUNK_SZ)   # (Y_RANGE, 256)

    is_air = (flat == 0)
    fluid_lut = np.zeros(len(pal._names), dtype=bool)
    for _fn in _FLUID_NAMES:
        _fi = pal._idx.get(_fn)
        if _fi is not None:
            fluid_lut[_fi] = True
    is_fluid = fluid_lut[flat]

    def _highest_yi(solid_mask: np.ndarray) -> np.ndarray:
        """solid_mask: (Y_RANGE, 256) bool — return (256,) stored heightmap values."""
        flipped  = solid_mask[::-1, :]
        has_any  = solid_mask.any(axis=0)
        first_hi = np.argmax(flipped, axis=0)
        highest  = np.where(has_any, Y_RANGE - 1 - first_hi, 0)
        return highest.astype(np.int64)          # stored = yi = MC_Y - Y_MIN

    def _pack_hm(values: np.ndarray) -> "nbtlib.LongArray":
        bpe     = _math.ceil(_math.log2(Y_RANGE + 1))
        vpl     = 64 // bpe                      # 7 values/long @ 9bpe; 6 @ 10bpe
        n_longs = _math.ceil(len(values) / vpl)  # 37 @ 9bpe; 43 @ 10bpe
        pad     = n_longs * vpl - len(values)
        padded  = np.concatenate([values, np.zeros(pad, dtype=np.int64)])
        reshaped    = padded.reshape(n_longs, vpl)
        bit_offsets = (np.arange(vpl, dtype=np.int64) * bpe).reshape(1, vpl)
        longs = np.bitwise_or.reduce(reshaped << bit_offsets, axis=1)
        return nbtlib.LongArray(longs.tolist())

    ws = _highest_yi(~is_air)                    # water counts as surface
    of = _highest_yi(~is_air & ~is_fluid)        # highest non-fluid solid

    return nbtlib.Compound({
        "WORLD_SURFACE":             _pack_hm(ws),
        "OCEAN_FLOOR":               _pack_hm(of),
        "MOTION_BLOCKING":           _pack_hm(ws),
        "MOTION_BLOCKING_NO_LEAVES": _pack_hm(of),  # no leaves in terrain yet
    })


def _chunk_to_nbt_bytes(
    cx: int, cz: int,
    vol: np.ndarray,            # (Y_RANGE, tile_h, tile_w) uint16 palette indices
    pal: BlockPalette,          # palette for index → name conversion
    biome_grid: np.ndarray,     # (tile_h, tile_w) Vandir biome name strings
    tile_world_x: int, tile_world_z: int,
    tile_h: int, tile_w: int,
    river_water_y: np.ndarray | None = None,  # (tile_h, tile_w) int16 — river water surface (fluid-tick river/ocean split)
    gap_mask: np.ndarray | None = None,   # (tile_h, tile_w) gap_mask — stony_peaks rock-snow suppression
    cfg: dict | None = None,
) -> bytes:
    """Return zlib-compressed NBT bytes for one 16×16-column chunk."""
    import nbtlib
    CHUNK_SZ = 16

    # Clip this chunk's block range to tile bounds
    bx_off = cx * CHUNK_SZ - tile_world_x
    bz_off = cz * CHUNK_SZ - tile_world_z
    bx_lo = max(0, bx_off);    bx_hi = min(tile_w, bx_off + CHUNK_SZ)
    bz_lo = max(0, bz_off);    bz_hi = min(tile_h, bz_off + CHUNK_SZ)
    lx_lo = bx_lo - bx_off;    lx_hi = bx_hi - bx_off   # local X within chunk
    lz_lo = bz_lo - bz_off;    lz_hi = bz_hi - bz_off   # local Z within chunk

    # Extract chunk volume slice as uint16 (Y_RANGE, 16, 16)
    chunk_u16 = np.zeros((Y_RANGE, CHUNK_SZ, CHUNK_SZ), dtype=np.uint16)
    if bx_hi > bx_lo and bz_hi > bz_lo:
        chunk_u16[:, lz_lo:lz_hi, lx_lo:lx_hi] = vol[:, bz_lo:bz_hi, bx_lo:bx_hi]

    # S100 perf: NO string materialisation.  Everything downstream (palette
    # build, all-air checks, solid masks, heightmaps, edge water ticks) now
    # works directly on the uint16 chunk_u16 — pal.to_strings on the full
    # (Y_RANGE,16,16) slab plus the object-dtype compares it fed were the
    # dominant emit cost.  Index 0 is always "air" (BlockPalette invariant).

    # Vandir biome (Z,X) grid for this chunk — default _DEFAULT for out-of-tile area
    biome_vandir_zx = np.full((CHUNK_SZ, CHUNK_SZ), "_DEFAULT", dtype=object)
    if bx_hi > bx_lo and bz_hi > bz_lo:
        biome_vandir_zx[lz_lo:lz_hi, lx_lo:lx_hi] = biome_grid[bz_lo:bz_hi, bx_lo:bx_hi]

    # S66/S67: altitude remap — pixels whose surface_y exceeds a threshold
    # and whose biome matches a remap's source get reassigned to the target
    # biome name for the MC tag emit.  S67 extends with a mountaincap dither
    # zone for SNOWY_BOREAL_TAIGA in Y[200, 250].
    if BIOME_ALTITUDE_REMAPS:
        _solid_for_remap = (chunk_u16 != 0)
        _has_any_r = _solid_for_remap.any(axis=0)
        _first_hi_r = np.argmax(_solid_for_remap[::-1, :, :], axis=0)
        _surf_yi_r = np.where(_has_any_r, Y_RANGE - 1 - _first_hi_r, 0)
        _surf_wy_r = _surf_yi_r + Y_MIN  # (16, 16) world Y
        for _remap in BIOME_ALTITUDE_REMAPS:
            src, tgt, thr = _remap["source"], _remap["target"], _remap["threshold"]
            _hit = (biome_vandir_zx == src) & (_surf_wy_r >= thr)
            if _hit.any():
                biome_vandir_zx[_hit] = tgt

        # S68: SBT mountaincap dither moved to surface_decorator so snow
        # carpet AND biome tag decisions are SYNCED.  The biome_grid passed
        # in here already has the remap applied; no duplicate logic here.

    # Downsample Vandir biomes to (4, 4) biome-cell quanta
    biome_vandir_q = biome_vandir_zx[::4, ::4]  # (4, 4)

    # S95-T4: ocean-first fallback. An empty / "ocean" / "_OCEAN" biome resolves
    # to minecraft:ocean BEFORE the _DEFAULT(plains) fallback. Mainland never
    # emits these (assign_biomes fills every cell with a named biome or "_OCEAN",
    # which is already a BIOME_TO_MC key, and out-of-tile padding is "_DEFAULT"),
    # so this only catches the currently-wrong island / far-ocean case.
    _OCEAN_BIOME = BIOME_TO_MC["_OCEAN"]   # "minecraft:ocean"
    def _mc_ground(b) -> str:
        s = str(b)
        hit = BIOME_TO_MC.get(s)
        if hit is not None:
            return hit
        if s in ("", "ocean", "_OCEAN"):
            return _OCEAN_BIOME
        return BIOME_TO_MC["_DEFAULT"]

    # Ground MC biome per-cell (used for cells at or below surface)
    ground_q = np.vectorize(_mc_ground)(biome_vandir_q)  # (4, 4) MC biome strings

    # Sky MC biome per-cell (cells strictly above surface).  Falls back to
    # ground biome when Vandir biome not in BIOME_TO_MC_SKY.
    def _sky_of(b: str) -> str:
        return BIOME_TO_MC_SKY.get(str(b), _mc_ground(b))
    sky_q = np.vectorize(_sky_of)(biome_vandir_q)  # (4, 4)

    # S89: STONY_PEAKS runtime-snow suppression. MC re-snows bare rock during
    # weather (coldEnoughToSnow at altitude), undoing the bare cliffs we kept
    # snow-free. Emit minecraft:stony_peaks (temp 1.0 -> never cold enough to
    # snow even at peak altitude) for cells whose surface is rock (gap==5 + a
    # small adjacency) AND whose Vandir biome is snowy. Tightly gated so the
    # (unavoidable) 4x4 biome tint only appears in the immediate snowy-rock
    # footprint. Overrides BOTH ground+sky so the precipitation query (sky cell
    # at surface+1) and the fast path both read stony_peaks.
    if gap_mask is not None and cfg is not None:
        _sp_cfg = cfg.get("snow_physics", {}) if isinstance(cfg, dict) else {}
        _sp_biome = _sp_cfg.get("rock_runtime_biome", "")
        _snowy = set(_sp_cfg.get("runtime_snowy_biomes", []))
        if _sp_biome and _snowy:
            _gap_zx = np.zeros((CHUNK_SZ, CHUNK_SZ), dtype=np.uint8)
            if bx_hi > bx_lo and bz_hi > bz_lo:
                _gap_zx[lz_lo:lz_hi, lx_lo:lx_hi] = gap_mask[bz_lo:bz_hi, bx_lo:bx_hi]
            _rock = (_gap_zx == 5)
            if _rock.any():
                _dil = int(_sp_cfg.get("rock_runtime_dilate", 2))
                if _dil > 0:
                    from scipy.ndimage import binary_dilation as _bd_sp
                    _rock = _bd_sp(_rock, iterations=_dil)
                # cell (4x4) is rock if ANY rock-adjacent pixel falls in it
                _rock_q = _rock.reshape(4, 4, 4, 4).any(axis=(1, 3))   # (4, 4)
                _snowy_q = np.array(
                    [[str(biome_vandir_q[i, j]) in _snowy for j in range(4)]
                     for i in range(4)], dtype=bool)
                _sp_cells = _rock_q & _snowy_q
                if _sp_cells.any():
                    ground_q = ground_q.copy(); sky_q = sky_q.copy()
                    ground_q[_sp_cells] = _sp_biome
                    sky_q[_sp_cells] = _sp_biome

    # Fast path: if every 4×4 patch has sky == ground (no BOREAL_ALPINE etc.
    # in this chunk), skip the per-cell Y split entirely.
    sky_active = not np.array_equal(sky_q, ground_q)

    # Per-patch surface world-Y (MIN across 4×4 block patch — S63 fix).
    # MAX was too permissive: snow queries from LOWER surfaces in a mixed-height
    # patch landed in ground (taiga) cells and snow still fell on BOREAL_ALPINE.
    # MIN guarantees every cell that CONTAINS any surface becomes sky — the
    # trade-off is more grass cells get plains tint on mixed-height patches.
    solid_mask = (chunk_u16 != 0)                     # (Y_RANGE, 16, 16)
    has_any    = solid_mask.any(axis=0)               # (16, 16)
    # argmax on reversed axis → index of first solid from top
    first_hi   = np.argmax(solid_mask[::-1, :, :], axis=0)
    surface_yi = np.where(has_any, Y_RANGE - 1 - first_hi, 0)  # (16, 16) vol-idx
    surface_wy = surface_yi + Y_MIN                            # (16, 16) world-Y
    # Air-only columns get a SENTINEL (max int32) so they don't drag MIN down.
    _SENTINEL_WY = np.iinfo(np.int32).max
    sentinel_wy  = np.where(has_any, surface_wy.astype(np.int32), _SENTINEL_WY)
    # MIN per 4×4 patch → every cell that contains *any* surface becomes sky.
    surface_wy_q = sentinel_wy.reshape(4, 4, 4, 4).min(axis=(1, 3))  # (4, 4)
    # All-air patches (MIN == SENTINEL) fall through to Y_MIN so every cell sky.
    surface_wy_q = np.where(surface_wy_q == _SENTINEL_WY, Y_MIN, surface_wy_q)
    surface_wy_q = surface_wy_q.astype(np.int16)

    sections = []
    _sec_y_max = (_TEST_SECTION_Y_MAX + 1) if _TEST_SECTION_Y_MAX is not None \
                 else (_SECTION_Y_MIN + _N_SECTIONS)

    # S100 perf: uniform-biome fast path — when sky == ground the (4,4,4)
    # biome quanta are IDENTICAL for every section of this chunk, so the
    # biomes Compound is built once (value-cached) and the same instance is
    # shared by all sections (serialization re-reads it; bytes unchanged).
    _uniform_biomes_nbt = None
    if not sky_active:
        # Same values as the original per-section np.stack([ground_q] * 4).
        _uniform_biomes_nbt = _build_biomes_nbt_cached(
            np.stack([ground_q] * 4, axis=0))

    for sec_y in range(_SECTION_Y_MIN, _sec_y_max):
        yi_base = sec_y * CHUNK_SZ - Y_MIN   # vol Y-index of section's lowest block

        # S100 perf: section block data is a zero-copy uint16 VIEW of
        # chunk_u16 — the out-of-tile area is already 0 == "air", exactly
        # what the old per-section np.full("air") object buffer provided
        # (~50k np.full calls / 14s per dense tile, now gone).  Sections
        # overhanging the vol Y range (only possible with a non-default
        # _TEST_SECTION_Y_MAX / world-height change) get a zero-padded copy.
        yi_lo = max(0, yi_base);  yi_hi = min(Y_RANGE, yi_base + CHUNK_SZ)
        if yi_hi - yi_lo == CHUNK_SZ:
            sec_u16 = chunk_u16[yi_lo:yi_hi]
        elif yi_hi > yi_lo:
            sec_u16 = np.zeros((CHUNK_SZ, CHUNK_SZ, CHUNK_SZ), dtype=np.uint16)
            sec_u16[yi_lo - yi_base:yi_hi - yi_base] = chunk_u16[yi_lo:yi_hi]
        else:
            sec_u16 = _EMPTY_SEC_U16   # entirely outside vol range → all air
        # S84 all-air fast path, now an integer test (index 0 is always
        # "air" and BlockPalette.idx dedups, so no other index maps to it).
        _is_all_air = not sec_u16.any()

        # Build (4, 4, 4) biome quanta for this section — 4 vertical cells,
        # each covering a 4-block Y range.  Rule: cell painted sky iff
        # cell.bottom_Y >= surface_wy_q (per 4×4 patch max).  This guarantees
        # the MOTION_BLOCKING precipitation query (at surface+1) hits a sky
        # cell for biomes with a sky override.
        if sky_active:
            biome_q4 = np.empty((4, 4, 4), dtype=object)
            sec_bottom_wy = sec_y * CHUNK_SZ                # world Y of section bottom
            for yy in range(4):
                cell_bottom_wy = sec_bottom_wy + yy * 4     # scalar
                above = cell_bottom_wy >= surface_wy_q      # (4, 4) bool
                biome_q4[yy] = np.where(above, sky_q, ground_q)
            _biomes_nbt = _build_biomes_nbt_cached(biome_q4)
        else:
            # Uniform-column fast path (original S60 behaviour)
            _biomes_nbt = _uniform_biomes_nbt

        # S60: emit ALL sections, including fully-air ones. The biome_q4 tag
        # (desert/taiga/etc.) applies vertically through the entire column so
        # MC shows the correct biome label when the player flies above terrain.
        # REQUIRES the `vandir_height.zip` datapack (min_y=-64, height=768) in
        # the target world's `datapacks/` folder. Without the datapack MC uses
        # vanilla 1.21.10 height=384 (24 sections) and emitting 48 sections
        # triggers `ArrayIndexOutOfBoundsException: Index 47 out of bounds for
        # length 24` at chunk load.  S74 added auto-install in run_pipeline.py
        # to copy assets/vandir_height.zip into output/datapacks/.
        # S62: biome_q4 is now per-section — sky-biome override for
        # BOREAL_ALPINE and friends kills altitude snow without datapacks.
        # S84: fast path for all-air sections. Skip np.unique + _entry
        # parsing + Compound construction; reuse the cached air palette.
        _block_states_nbt = (
            _get_air_block_states_nbt() if _is_all_air
            else _build_block_states_nbt_u16(sec_u16, pal)
        )
        sections.append(nbtlib.Compound({
            "Y":            nbtlib.Byte(sec_y),
            "block_states": _block_states_nbt,
            "biomes":       _biomes_nbt,
            # SkyLight / BlockLight intentionally omitted — isLightOn=0 tells MC
            # to compute lighting itself, so providing arrays is unnecessary.
        }))

    # Pre-schedule fluid ticks for water blocks on tile-boundary chunk edges.
    # Without these, MC queues all edge-water updates on load → server stall.
    cx0_tile = tile_world_x // CHUNK_SZ
    cx1_tile = (tile_world_x + tile_w - 1) // CHUNK_SZ
    cz0_tile = tile_world_z // CHUNK_SZ
    cz1_tile = (tile_world_z + tile_h - 1) // CHUNK_SZ

    fluid_tick_list = []

    # S100 perf: edge columns are scanned in the index domain.  The old
    # string compare matched the literal name "water" only (not
    # "minecraft:water"), so the index compare uses that exact name; -1
    # sentinel (name absent from palette) can never equal a uint16 cell.
    _water_tick_idx = pal._idx.get("water", -1)

    def _add_water_ticks(edge_vol, lx_fixed, lz_fixed):
        """edge_vol: (Y_RANGE, 16) uint16 slice. lx_fixed/lz_fixed: scalar or None (other axis varies).
        Only ticks the topmost water block per column — interior water is stable and never needs ticking.

        S86: tick OCEAN ONLY by checking river_water_y per column.  Any column
        that has river_water_y > 0 is RIVER (including river deltas at SEA_Y)
        — never tick.  Otherwise it's ocean (or carved-out lake interior) —
        tick the topmost water block to stitch seams.
        Supersedes the S85 `world_y > SEA_Y` gate which leaked ticks at SEA_Y
        river-delta cells (user S86 feedback: "rivers still updating less but
        still happening").  River carver's water state is a stable source
        block; MC fluid ticks would flow it sideways into the carved trough
        and re-settle on chunk load."""
        for other in range(edge_vol.shape[1]):
            col = edge_vol[:, other]
            water_ys = np.where(col == _water_tick_idx)[0]
            if len(water_ys) == 0:
                continue
            yi = int(water_ys[-1])  # topmost water block only
            lx = lx_fixed if lx_fixed is not None else other
            lz = lz_fixed if lz_fixed is not None else other
            # Tile-local coords for river check.  river_water_y is a PARAMETER
            # of _chunk_to_nbt_bytes (threaded write_tile -> write_tile_to_region
            # -> here).  Earlier it was an undefined free var -> NameError on every
            # water-bearing tile-edge chunk -> silently swallowed -> MISSING river
            # chunks across the world.  Do not un-thread it.
            wx = cx * CHUNK_SZ + lx
            wz = cz * CHUNK_SZ + lz
            tile_col = wx - tile_world_x
            tile_row = wz - tile_world_z
            if (river_water_y is not None
                    and 0 <= tile_row < river_water_y.shape[0]
                    and 0 <= tile_col < river_water_y.shape[1]
                    and int(river_water_y[tile_row, tile_col]) > 0):
                continue  # S86: river column — never tick
            world_y = yi + Y_MIN
            if world_y > SEA_Y:
                continue  # S85 fallback: above-sea water without river_water_y
            fluid_tick_list.append(nbtlib.Compound({
                "i": nbtlib.String("minecraft:water"),
                "t": nbtlib.Int(0),
                "p": nbtlib.Int(0),
                "x": nbtlib.Int(wx),
                "y": nbtlib.Int(int(yi) + Y_MIN),
                "z": nbtlib.Int(wz),
            }))

    if cx == cx0_tile:  # west edge — neighbour chunk (cx-1) is outside tile
        _add_water_ticks(chunk_u16[:, :, 0],  lx_fixed=0,  lz_fixed=None)
    if cx == cx1_tile:  # east edge — neighbour chunk (cx+1) is outside tile
        _add_water_ticks(chunk_u16[:, :, 15], lx_fixed=15, lz_fixed=None)
    if cz == cz0_tile:  # north edge — neighbour chunk (cz-1) is outside tile
        _add_water_ticks(chunk_u16[:, 0, :],  lx_fixed=None, lz_fixed=0)
    if cz == cz1_tile:  # south edge — neighbour chunk (cz+1) is outside tile
        _add_water_ticks(chunk_u16[:, 15, :], lx_fixed=None, lz_fixed=15)

    fluid_ticks_nbt = (nbtlib.List[nbtlib.Compound](fluid_tick_list)
                       if fluid_tick_list else nbtlib.List([]))

    root = nbtlib.Compound({
        "DataVersion":    nbtlib.Int(_CHUNK_DATA_VERSION),
        "xPos":           nbtlib.Int(cx),
        "yPos":           nbtlib.Int(_SECTION_Y_MIN),
        "zPos":           nbtlib.Int(cz),
        "Status":         nbtlib.String("minecraft:full"),
        "LastUpdate":     nbtlib.Long(0),
        "InhabitedTime":  nbtlib.Long(0),
        "sections":       nbtlib.List[nbtlib.Compound](sections),
        "block_entities": nbtlib.List([]),
        "fluid_ticks":    fluid_ticks_nbt,
        "block_ticks":    nbtlib.List([]),
        "PostProcessing": nbtlib.List[nbtlib.List]([nbtlib.List([]) for _ in sections]),
        "Heightmaps":     _build_heightmaps_nbt_u16(chunk_u16, pal),
        "structures":     nbtlib.Compound({
            "References": nbtlib.Compound({}),
            "starts":     nbtlib.Compound({}),
        }),
        "isLightOn":      nbtlib.Byte(0),
    })

    f = nbtlib.File(gzipped=False, root_name="")
    f.update(root)
    buf = _io.BytesIO()
    f.write(buf, byteorder="big")
    return _zlib.compress(buf.getvalue())


def write_tile_to_region(
    vol:          np.ndarray,          # (Y_RANGE, H, W) uint16 palette indices
    pal:          BlockPalette,        # palette for index → name conversion
    biome_grid:   np.ndarray,          # (H, W) Vandir biome name strings
    tile_world_x: int,                 # world X of tile origin (blocks)
    tile_world_z: int,                 # world Z of tile origin (blocks)
    output_dir:   Path,
    tile_h:       int,
    tile_w:       int,
    river_water_y: np.ndarray | None = None,  # (tile_h, tile_w) int16 — river water surface; fluid-tick river/ocean split
    gap_mask:     np.ndarray | None = None,   # (H, W) uint8 gap_mask — for stony_peaks rock-snow suppression
    cfg:          dict | None = None,
) -> list[str]:
    """
    Write the tile volume to a .mca region file.

    Each 512×512-block tile maps to exactly one region file (r.TX.TZ.mca).
    Uses nbtlib + direct Anvil format — no amulet dependency.
    Produces Java 1.21.10 chunks (DataVersion 4556) with 32 vertical sections.

    Returns list of written region file paths.
    """
    # S62: Vandir biome grid is passed to _chunk_to_nbt_bytes directly — the
    # per-chunk MC translation happens there because the sky-biome override
    # needs per-cell surface Y to decide ground-vs-sky.  Previous S60 code
    # pre-translated Vandir → MC here via BIOME_TO_MC lookup; that path
    # cannot differentiate sky cells.

    # Chunk range covering this tile
    CHUNK_SZ = 16
    cx0 = tile_world_x // CHUNK_SZ
    cx1 = (tile_world_x + tile_w - 1) // CHUNK_SZ
    cz0 = tile_world_z // CHUNK_SZ
    cz1 = (tile_world_z + tile_h - 1) // CHUNK_SZ

    # Region for this tile (512px tile = 1 region exactly for full tiles)
    rx = tile_world_x // 512
    rz = tile_world_z // 512

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    mca_path = output_dir / f"r.{rx}.{rz}.mca"

    loc_table  = bytearray(_SECTOR_SZ)
    time_table = bytearray(_SECTOR_SZ)
    sectors    = bytearray()
    next_sec   = 2   # first chunk starts after the 2-sector (8192-byte) header

    log_path   = Path(output_dir) / "chunk_errors.log"
    _err_lines = []

    for cx in range(cx0, cx1 + 1):
        for cz in range(cz0, cz1 + 1):
            try:
                compressed = _chunk_to_nbt_bytes(
                    cx, cz, vol, pal, biome_grid,
                    tile_world_x, tile_world_z, tile_h, tile_w,
                    river_water_y=river_water_y,
                    gap_mask=gap_mask, cfg=cfg,
                )
            except Exception as _exc:
                tb = _traceback.format_exc()
                msg = f"CHUNK FAILED cx={cx} cz={cz}: {_exc}\n{tb}"
                print(msg, flush=True)
                _err_lines.append(msg)
                continue   # non-fatal: skip broken chunk

            # Chunk entry: 4-byte length + 1-byte compression type (2=zlib) + data
            entry  = _struct.pack(">I", len(compressed) + 1) + b"\x02" + compressed
            pad    = (_SECTOR_SZ - len(entry) % _SECTOR_SZ) % _SECTOR_SZ
            entry += b"\x00" * pad
            n_sec  = len(entry) // _SECTOR_SZ

            # Location table: index = (cz & 31) * 32 + (cx & 31)
            loc_idx = (cz & 31) * 32 + (cx & 31)
            _struct.pack_into(">I", loc_table, loc_idx * 4, (next_sec << 8) | n_sec)
            sectors  += entry
            next_sec += n_sec

    with open(mca_path, "wb") as fh:
        fh.write(loc_table)
        fh.write(time_table)
        fh.write(sectors)

    if _err_lines:
        with open(log_path, "w", encoding="utf-8") as lf:
            lf.write(f"Chunk errors for r.{rx}.{rz}.mca\n{'='*60}\n")
            lf.write("\n".join(_err_lines))
        print(f"[chunk_writer] {len(_err_lines)} chunk(s) failed — see {log_path}", flush=True)
    else:
        print(f"[chunk_writer] All chunks OK for r.{rx}.{rz}.mca", flush=True)

    return [str(mca_path)]


# ---------------------------------------------------------------------------
# HIGH-LEVEL TILE WRITER  (called by run_pipeline.py)
# ---------------------------------------------------------------------------

def write_tile(
    surface_y:     np.ndarray,         # (H, W) int16
    surface_blk:   np.ndarray,         # (H, W) str
    sub_blk:       np.ndarray,         # (H, W) str
    ground_cover:  np.ndarray,         # (H, W) str
    biome_grid:    np.ndarray,         # (H, W) str
    placements:    list,               # list[PlacementRecord] from Step 8
    schem_loader,                      # schematic_loader module / callable
    tile_world_x:  int,
    tile_world_z:  int,
    output_dir:    Path,
    cfg:           dict | None = None, # thresholds.json content (for geo params)
    river_water_y: np.ndarray | None = None, # (H, W) int16 — river water surface
    lithology_tile: np.ndarray | None = None, # (H, W) uint8 — Phase 1.75
    flow_tile:      np.ndarray | None = None, # (H, W) float32 — Phase 1.75
    gap_mask:       np.ndarray | None = None, # (H, W) uint8 — walk #9 cleanup
    river_meta:     np.ndarray | None = None, # (H, W) uint8 — channel type (3=lake) for steep-void seal
) -> list[str]:
    """
    Full tile write pipeline:
      1. Build volume array from terrain data (with cliff interior banding
         OR geology-stratified fill when lithology is enabled)
      2. Stamp schematics
      3. Write to .mca region files

    Returns list of region file paths written.
    """
    H, W = surface_y.shape

    # S89: tree/schematic anchoring OVERRIDES surface-decorator ground cover.
    # Clear ground_cover in a small box at each placement anchor BEFORE the
    # column build writes it, so no grass / snow_carpet / tall veg sits at the
    # trunk base (the "floating tree on snow/vegetation" regression). The stamp
    # already overwrites the trunk column itself; this also clears the immediate
    # base ring so nothing pokes out beside a sunk trunk.
    if placements:
        for _p in placements:
            _lx = int(getattr(_p, "world_x", 0)) - tile_world_x
            _lz = int(getattr(_p, "world_z", 0)) - tile_world_z
            _z0 = max(0, _lz - 2); _z1 = min(H, _lz + 3)
            _x0 = max(0, _lx - 2); _x1 = min(W, _lx + 3)
            if _z1 > _z0 and _x1 > _x0:
                ground_cover[_z0:_z1, _x0:_x1] = ""

    # Check whether the geology feature flag is ON
    _lith_cfg = (cfg or {}).get("lithology", {})
    _use_geo  = bool(_lith_cfg.get("feature_flag_enabled", False))

    # Step 1 — build volume
    _cb   = (cfg or {}).get("cliff_banding", {})
    _cthr = float(_cb.get("cliff_deg_thr", 45.0))
    _bsy  = int(_cb.get("band_scale_y", 12))
    vol, pal = build_column_array(
        surface_y, surface_blk, sub_blk, ground_cover,
        biome_grid     = biome_grid,
        cliff_deg_thr  = _cthr,
        band_scale_y   = _bsy,
        tile_world_x   = tile_world_x,
        tile_world_z   = tile_world_z,
        river_water_y  = river_water_y,
        lithology_tile = lithology_tile if _use_geo else None,
        use_new_geology= _use_geo,
        flow_tile      = flow_tile if _use_geo else None,
        cfg            = cfg if _use_geo else None,
        gap_mask       = gap_mask,
        river_meta     = river_meta,
    )

    # S71-2 Option β: river water-spread post-pass (purely additive — does
    # not modify terrain).  For every river pixel, look at 4-neighbours up to
    # 2 blocks away.  If a neighbour column has air at water_y AND solid
    # land at (water_y - 1), place water there.  Effect: river water spills
    # outward into downhill-bank airspace, filling cross-section gaps where
    # natural terrain is below the water surface.  Container: only fills
    # cells that have land directly below water_y, so water cannot flow over
    # cliffs or into deeper terrain — it only completes the river's wet
    # cross-section where the carve missed it.  Replaces S71's connected-
    # component carve mod (which made rivers look "less natural").
    if river_water_y is not None and (river_water_y > 0).any():
        try:
            water_idx = pal.idx("water")
            air_idx   = pal.air
            src_z, src_x = np.where(river_water_y > 0)
            src_wy = river_water_y[src_z, src_x].astype(np.int64)
            # 2-block horizontal spread (radii 1 and 2, all 4 directions per
            # radius).  Skip neighbours that are themselves river pixels and
            # neighbours where vol[water_y] isn't air or vol[water_y-1] isn't
            # solid.
            for r_step in (1, 2):
                for dz, dx in ((-r_step, 0), (r_step, 0), (0, -r_step), (0, r_step)):
                    tz = src_z + dz
                    tx = src_x + dx
                    twy = src_wy
                    inb = (tz >= 0) & (tz < H) & (tx >= 0) & (tx < W)
                    tz, tx, twy = tz[inb], tx[inb], twy[inb]
                    if len(tz) == 0:
                        continue
                    # Skip neighbours that are themselves river pixels
                    nb_is_river = river_water_y[tz, tx] > 0
                    keep = ~nb_is_river
                    tz, tx, twy = tz[keep], tx[keep], twy[keep]
                    if len(tz) == 0:
                        continue
                    yi = twy - Y_MIN
                    yi_ok = (yi >= 1) & (yi < Y_RANGE)
                    tz, tx, yi = tz[yi_ok], tx[yi_ok], yi[yi_ok]
                    if len(tz) == 0:
                        continue
                    # vol indexed (Y, Z, X)
                    cur = vol[yi, tz, tx]
                    below = vol[yi - 1, tz, tx]
                    can_spread = (cur == air_idx) & (below != air_idx) & (below != water_idx)
                    if can_spread.any():
                        z_ok  = tz[can_spread]
                        x_ok  = tx[can_spread]
                        yi_ok = yi[can_spread]
                        vol[yi_ok, z_ok, x_ok] = water_idx
        except Exception as _e:
            print(f"  [warn] river water-spread post-pass failed: {_e}", flush=True)

    # S71: Build water_col_mask for tree footprint reject — catches carved
    # river channels above sea level (river_water_y > 0) AND ocean (sy < 63).
    # The placement-time 14px buffer in schematic_placement should already
    # exclude these, but per-column reject in stamp_schematic is defence
    # in depth: any tree whose footprint touches a water column → whole-stamp
    # reject.
    if river_water_y is not None:
        water_col_mask = (river_water_y > 0) | (surface_y < 63)
    else:
        water_col_mask = (surface_y < 63)

    # Step 2 — stamp schematics
    _clip_oob = bool((cfg or {}).get("tree_seam", {}).get("clip_oob", True))  # S95 tree-seam STEP A
    _band_px_cw = int((cfg or {}).get("tree_seam", {}).get("band_px", 16))    # S100 seam-band
    for p in placements:
        local_x = p.world_x - tile_world_x
        local_z = p.world_z - tile_world_z
        _band_p = bool(getattr(p, "band", False))
        if _band_p:
            # S100 seam-band tree: emitted identically by BOTH adjacent tiles;
            # the anchor may be OOB (neighbour's territory).  Stamp the
            # IN-BOUNDS portion via per-column clipping instead of anchor-
            # culling, and seat deterministically (whole-stamp sink/reject
            # decisions see only in-bounds columns → would desync the halves).
            if not (-_band_px_cw <= local_x < W + _band_px_cw
                    and -_band_px_cw <= local_z < H + _band_px_cw):
                continue
        elif not (0 <= local_x < W and 0 <= local_z < H):
            continue  # outside tile bounds (edge overlap)
        try:
            schem_data = schem_loader.load_schem(Path(p.schem_path))
            schem_data._rotation = getattr(p, 'rotation', 0)
            stamp_schematic(vol, pal, schem_data, local_x, local_z,
                           p.place_y, surface_y=surface_y,
                           water_col_mask=water_col_mask, clip_oob=_clip_oob,
                           deterministic_seat=_band_p)
        except Exception as e:
            import traceback
            print(f"  [warn] schematic stamp failed: {p.schem_path}: {e}",
                  flush=True)
            traceback.print_exc()

    # S71 — Snow-on-trees post-pass for snowy biomes per user walk feedback.
    # In SNOWY_BOREAL_TAIGA, ARCTIC_TUNDRA, and FROZEN_FLATS, drop a snow_layer
    # block on top of every leaf-block column that doesn't already have one.
    # NOT applied to BOREAL_ALPINE (user explicit: keep it snow-free) and not
    # to SAND_DUNE_DESERT or other dry/wet biomes.  Applied AFTER all schematic
    # stamps so it covers tree canopies, not before (would be overwritten).
    # S71-3: FF removed — user explicit "no snowcover on pines" for FF.
    # Snow on FF surface comes from explicit snow_layer GC entries only.
    _SNOWY_BIOMES = frozenset({"SNOWY_BOREAL_TAIGA", "ARCTIC_TUNDRA"})
    snowy_mask = np.zeros((H, W), dtype=bool)
    for _sb in _SNOWY_BIOMES:
        snowy_mask |= (biome_grid == _sb)
    # S88: no snow on tree canopies above steep slopes -- physically snow
    # doesn't accumulate on near-vertical faces, and the new Norterre
    # cliff-rock aesthetic needs to read as rock not snow-frosted rock.
    # Slope max comes from snow_carpet.slope_max_deg (default 35°).
    if snowy_mask.any():
        from core.eco_gradients import compute_cliff_deg as _ccd_snowsnow
        _snowsnow_slope = _ccd_snowsnow(surface_y)
        _snow_slope_max = float(
            cfg.get("snow_carpet", {}).get("slope_max_deg", 35.0)
        ) if cfg else 35.0
        snowy_mask &= (_snowsnow_slope < _snow_slope_max)
        del _snowsnow_slope
    if snowy_mask.any():
        # Build leaf-index set from palette.  Includes vanilla leaf families +
        # azalea variants — anything that should accumulate snow on top.
        _LEAF_NAMES = (
            "oak_leaves", "spruce_leaves", "birch_leaves", "jungle_leaves",
            "acacia_leaves", "dark_oak_leaves", "mangrove_leaves",
            "cherry_leaves", "azalea_leaves", "flowering_azalea_leaves",
            "pale_oak_leaves",
        )
        leaf_indices: set[int] = set()
        for _ln in _LEAF_NAMES:
            try:
                leaf_indices.add(pal.idx(_ln))
            except Exception:
                continue
        if leaf_indices:
            snow_idx = pal.idx("snow")
            air_idx  = pal.air  # @property, not callable
            # Find topmost leaf per (z, x).  vol = (Y_RANGE, H, W).
            # Iterate Y top-down (vol indexing) and place snow at top_leaf+1
            # if currently air.
            leaf_mask_vol = np.isin(vol, list(leaf_indices))  # (Y_RANGE, H, W) bool
            has_leaf = leaf_mask_vol.any(axis=0)  # (H, W)
            apply = has_leaf & snowy_mask
            if apply.any():
                # Topmost leaf Y-index per column (argmax on reversed)
                top_leaf_yi = (Y_RANGE - 1) - np.argmax(leaf_mask_vol[::-1, :, :], axis=0)
                rows, cols = np.where(apply)
                for r, c in zip(rows.tolist(), cols.tolist()):
                    yi = int(top_leaf_yi[r, c])
                    above = yi + 1
                    if 0 <= above < Y_RANGE and vol[above, r, c] == air_idx:
                        vol[above, r, c] = snow_idx

    # Step 3 — write to region files
    return write_tile_to_region(
        vol, pal, biome_grid,
        tile_world_x, tile_world_z,
        output_dir, H, W,
        river_water_y=river_water_y,
        gap_mask=gap_mask, cfg=cfg,
    )


# ---------------------------------------------------------------------------
# SMOKE TEST (stdlib + numpy only — no amulet, no schematic_loader)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("chunk_writer.py — smoke test")

    H, W = 32, 32  # small tile for speed
    rng = np.random.default_rng(42)

    surface_y   = np.full((H, W), 70, dtype=np.int16)
    surface_y[:H//4, :] = 40   # sub-sea section
    surface_y[H//4:H//2, :] = 63  # at sea level

    surface_blk  = np.full((H, W), "grass_block", dtype=object)
    surface_blk[:H//4, :] = "sand"
    sub_blk      = np.full((H, W), "dirt",        dtype=object)
    ground_cover = np.full((H, W), "",             dtype=object)
    ground_cover[H//2:, W//2:] = "short_grass"

    # Build volume
    vol, pal = build_column_array(surface_y, surface_blk, sub_blk, ground_cover)

    assert vol.shape == (Y_RANGE, H, W), f"vol shape {vol.shape}"
    assert vol.dtype == np.uint16, f"vol dtype {vol.dtype} — expected uint16"

    # Bedrock check
    assert np.all(vol[0, :, :] == pal.idx("bedrock")), "bedrock layer missing"

    # Water fill check — sub-sea columns should have water above surface
    sub_sea_col_y = int(surface_y[0, 0])  # = 40
    water_idx = pal.idx("water")
    for yi in range(sub_sea_col_y + 1 - Y_MIN, SEA_Y + 1 - Y_MIN):
        assert vol[yi, 0, 0] == water_idx, \
            f"missing water at yi={yi} (MC Y={yi+Y_MIN})"

    # Surface block check
    surf_yi = 70 - Y_MIN
    assert vol[surf_yi, H//2, 0] == pal.idx("grass_block"), "surface block wrong"

    # Ground cover check
    cover_yi = 70 + 1 - Y_MIN
    assert vol[cover_yi, H//2, W//2] == pal.idx("short_grass"), "ground cover wrong"

    # Stub schematic stamp
    fake_schem = {
        "blocks": [
            (0, 0, 0, None, "minecraft:oak_log", {}),
            (0, 1, 0, None, "minecraft:oak_log", {}),
            (0, 2, 0, None, "minecraft:oak_leaves", {}),
            (0, 0, 0, None, "minecraft:air", {}),   # air should be skipped
        ]
    }
    stamp_schematic(vol, pal, fake_schem, local_x=5, local_z=5, place_y=70)
    assert vol[70 - Y_MIN, 5, 5] == pal.idx("oak_log"),    "schematic stamp failed (y0)"
    assert vol[71 - Y_MIN, 5, 5] == pal.idx("oak_log"),    "schematic stamp failed (y1)"
    assert vol[72 - Y_MIN, 5, 5] == pal.idx("oak_leaves"), "schematic stamp failed (y2)"

    # BIOME_TO_MC completeness — every key in BIOME_TO_MC should have a value
    for k, v in BIOME_TO_MC.items():
        assert v.startswith("minecraft:"), f"biome {k} has invalid MC string: {v}"

    mem_mb = vol.nbytes / (1024 * 1024)
    print(f"  volume shape      : {vol.shape}  dtype={vol.dtype}  ({mem_mb:.1f} MB)")
    print(f"  palette entries   : {len(pal._names)}")
    print(f"  bedrock layer     : OK")
    print(f"  water fill        : OK (sub-sea col surface_y={sub_sea_col_y})")
    print(f"  surface block     : OK")
    print(f"  ground cover      : OK")
    print(f"  schematic stamp   : OK (3 blocks placed, air skipped)")
    print(f"  BIOME_TO_MC keys  : {len(BIOME_TO_MC)} entries, all valid")
    print("PASS")
    sys.exit(0)
