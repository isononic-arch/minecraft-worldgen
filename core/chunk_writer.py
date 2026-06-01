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
    "BOREAL_TAIGA":            "minecraft:meadow",  # S85: was stony_shore (S71). Meadow temp=0.5 keeps it snow-free + clean differentiation from BA plains
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

    __slots__ = ("_names", "_idx")

    def __init__(self):
        self._names: list[str] = ["air"]   # index 0 is always air
        self._idx: dict[str, int] = {"air": 0}

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

    row_u = (np.arange(H, dtype=np.uint32) + tile_world_z)
    col_u = (np.arange(W, dtype=np.uint32) + tile_world_x)

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
    rng = np.random.default_rng(seed)
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
    row_u = (np.arange(H, dtype=np.uint32) + tile_world_z)
    col_u = (np.arange(W, dtype=np.uint32) + tile_world_x)
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
        col_x = ((np.arange(W, dtype=np.uint32) + tile_world_x) // col_size)
        col_z = ((np.arange(H, dtype=np.uint32) + tile_world_z) // col_size)
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
    _noise_rng_default = np.random.default_rng(tile_world_x * 73856093 ^ tile_world_z * 19349669)

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
            (tile_world_x * 7919) ^ (tile_world_z * 31337) ^ 0xC0FFEE
        )
        stalk_heights = _kelp_rng.integers(5, 16, size=len(kr))   # [5, 15]
        for i in range(len(kr)):
            r, c = int(kr[i]), int(kc[i])
            sy_base = int(surface_y[r, c])
            if sy_base >= SEA_Y:
                continue  # not actually underwater
            top_y = min(sy_base + int(stalk_heights[i]), SEA_Y - 1)
            stalk_lo = sy_base + 1
            if top_y <= stalk_lo:
                continue
            # Fill Y = sy+1 .. top_y-1 with kelp_plant
            for wy in range(stalk_lo, top_y):
                yi = wy - Y_MIN
                if 0 <= yi < Y_RANGE:
                    vol[yi, r, c] = KELP_PLANT_IDX
            # Top: mature kelp
            top_yi = top_y - Y_MIN
            if 0 <= top_yi < Y_RANGE:
                vol[top_yi, r, c] = KELP_TOP_IDX

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

            # ~20% of river pixels with depth >= 2 get seagrass
            sg_place = river_px & (_sg_norm < 0.20) & (water_depth >= 2)
            sg_rows, sg_cols = np.where(sg_place)
            for r, c in zip(sg_rows, sg_cols):
                bed_yi = int(surface_y[r, c]) + 1 - Y_MIN
                # Only place seagrass if the block above is actually water
                if 0 <= bed_yi < Y_RANGE and bed_yi + 1 < Y_RANGE:
                    if vol[bed_yi + 1, r, c] == WATER_IDX:
                        vol[bed_yi, r, c] = SEAGRASS_IDX

        # Safety: ensure no air pockets in river water columns
        # Any non-water, non-seagrass block between surface and water_y → water
        if river_px.any():
            rp_rows, rp_cols = np.where(river_px)
            for r, c in zip(rp_rows, rp_cols):
                sy_i = int(surface_y[r, c]) + 1 - Y_MIN
                wy_i = int(river_water_y[r, c]) - Y_MIN
                for yi in range(sy_i, min(wy_i + 1, Y_RANGE)):
                    blk_idx = vol[yi, r, c]
                    if blk_idx != WATER_IDX and blk_idx != SEAGRASS_IDX:
                        vol[yi, r, c] = WATER_IDX

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
        for i in range(len(r_idx)):
            r, c = int(r_idx[i]), int(c_idx[i])
            sy_i = int(sy_flat[i]) - Y_MIN
            cov_yi = sy_i + 1
            if cov_yi < 0 or cov_yi >= Y_RANGE or sy_i < 0:
                continue
            blk_at_cover = vol[cov_yi, r, c]
            if blk_at_cover in veg_indices:
                # S60: check both water AND air below — either is a floater.
                below_idx = vol[sy_i, r, c]
                if below_idx == WATER_IDX or below_idx == AIR_IDX:
                    vol[cov_yi, r, c] = AIR_IDX
                    # Also clear upper half if double-tall
                    if cov_yi + 1 < Y_RANGE and vol[cov_yi + 1, r, c] in veg_indices:
                        vol[cov_yi + 1, r, c] = AIR_IDX

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
                    _bad_arr = np.array(list(_bad_indices), dtype=np.int32)
                    for _idx in range(len(_rr)):
                        _r, _c = int(_rr[_idx]), int(_cc[_idx])
                        _gid_here = int(lithology_tile[_r, _c]) if lithology_tile.shape == _rock_zone.shape else 0
                        _rock_idx = _gid_to_rock_idx.get(_gid_here, _stone_idx_fb)
                        _sy_abs = int(surface_y[_r, _c])
                        # Check Y-2 down to Y-5 (4 blocks)
                        for _y_off in range(2, 6):
                            _y_abs = _sy_abs - _y_off
                            _y_rel = _y_abs - Y_MIN
                            if _y_rel < 0 or _y_rel >= Y_RANGE:
                                continue
                            _cur = int(vol[_y_rel, _r, _c])
                            if _cur in _bad_indices:
                                vol[_y_rel, _r, _c] = _rock_idx
                del _rock_zone

    return vol, pal


# ---------------------------------------------------------------------------
# SCHEMATIC STAMPER
# ---------------------------------------------------------------------------

def stamp_schematic(
    vol:        np.ndarray,   # (Y_RANGE, H, W) uint16 �� modified in-place
    pal:        BlockPalette, # palette for index ↔ name conversion
    schem_data,               # SchemData dataclass or legacy dict
    local_x:    int,          # tile-local column (0..W-1)
    local_z:    int,          # tile-local row    (0..H-1)
    place_y:    int,          # world MC Y of schematic origin
    surface_y:  np.ndarray | None = None,  # (H, W) int16 — terrain surface
    water_col_mask: np.ndarray | None = None,  # (H, W) bool — S71: water columns (river+ocean)
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
    # Caches for overwrite check (reset per call)
    _OVERWRITABLE_CACHE: set[int] = set()
    _PROTECTED_CACHE: set[int] = set()
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
        # SchemData dataclass — blocks is (Y, Z, X) object array
        blk_arr = schem_data.blocks
        # Apply rotation in XZ plane (axes 1=Z, 2=X)
        if hasattr(schem_data, '_rotation'):
            rot = schem_data._rotation
        else:
            rot = 0
        if rot > 0:
            blk_arr = np.rot90(blk_arr, k=rot, axes=(1, 2)).copy()
            # Rotate directional blockstate properties to match spatial rotation.
            # rot=1 (90° CW): north→east→south→west→north
            # rot=2 (180°): north↔south, east↔west
            # rot=3 (270° CW): north→west→south→east→north
            _DIR_REMAP = {
                1: {"north": "east", "east": "south", "south": "west", "west": "north"},
                2: {"north": "south", "south": "north", "east": "west", "west": "east"},
                3: {"north": "west", "west": "south", "south": "east", "east": "north"},
            }
            remap = _DIR_REMAP.get(rot % 4)
            if remap:
                import re
                def _rotate_blockstate(name):
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
                # Apply to entire array (vectorized via unique values)
                unique_blocks = np.unique(blk_arr)
                for ub in unique_blocks:
                    s = str(ub)
                    if "[" in s:
                        rotated = _rotate_blockstate(s)
                        if rotated != s:
                            blk_arr[blk_arr == ub] = rotated
        sh, sl, sw = blk_arr.shape   # height, length (Z), width (X)

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
        MAX_TRUNK_EXT        = 6   # max blocks the extension will fill
        if surface_y is not None:
            col_reject = np.zeros((sl, sw), dtype=bool)
            col_desink = np.full((sl, sw), -(1 << 30), dtype=np.int64)
            # Precompute non-air + log masks in one pass via unique (much
            # cheaper than str() per cell).
            non_air = np.ones(blk_arr.shape, dtype=bool)
            is_log  = np.zeros(blk_arr.shape, dtype=bool)
            # Count logs by BARE name (no blockstate) so axis=x/y/z variants
            # of the same species aggregate → primary picks correct species.
            log_bare_counts: dict = {}
            for _u in np.unique(blk_arr):
                su = str(_u)
                if "air" in su:
                    non_air[blk_arr == _u] = False
                    continue
                if "log" in su or "wood" in su:
                    is_log[blk_arr == _u] = True
                    bare = su.replace("minecraft:", "").split("[")[0]
                    log_bare_counts[bare] = (
                        log_bare_counts.get(bare, 0) + int((blk_arr == _u).sum())
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
            runs = np.zeros((sl, sw), dtype=np.int32)
            for _sz in range(sl):
                for _sx in range(sw):
                    if not log_any_col[_sz, _sx]:
                        continue
                    first = int(lowest_log_sy_col[_sz, _sx])
                    if first > TRUNK_FIRST_MAX_Y:
                        continue  # logs start too high — branch, not trunk
                    run = 0
                    for _sy in range(first, sh):
                        if is_log[_sy, _sz, _sx]:
                            run += 1
                        else:
                            break
                    runs[_sz, _sx] = run
            max_run = int(runs.max()) if runs.size else 0
            trunk_threshold = max(TRUNK_RUN_MIN, int(max_run * TRUNK_RUN_FRAC))
            is_trunk_col = (runs >= trunk_threshold) & (runs > 0)
            has_trunks = bool(is_trunk_col.any())
            # Bbox out-of-tile reject + desink cutoff per column
            for _sz in range(sl):
                tz = local_z + _sz
                if not (0 <= tz < tile_H):
                    col_reject[_sz, :] = True
                    continue
                for _sx in range(sw):
                    tx = local_x + _sx
                    if not (0 <= tx < tile_W):
                        col_reject[_sz, _sx] = True
                        continue
                    col_desink[_sz, _sx] = int(surface_y[tz, tx])
                    # S64: footprint water gate — reject any column whose
                    # surface is below sea level so trees never leave leaves
                    # hanging over water from multi-column canopies.
                    # S71: extended to also reject river-water columns (carved
                    # rivers above sea level) via water_col_mask.  This catches
                    # trees with canopy hanging over a river even though the
                    # placement-time 14px buffer should prevent it — defence
                    # in depth.  Whole-stamp reject below trips on this too.
                    if col_desink[_sz, _sx] < 63:
                        col_reject[_sz, _sx] = True
                    elif water_col_mask is not None and water_col_mask[tz, tx]:
                        col_reject[_sz, _sx] = True
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
            _uwater_hit = False
            for _sz in range(sl):
                if _uwater_hit:
                    break
                for _sx in range(sw):
                    if not col_reject[_sz, _sx]:
                        continue
                    if not non_air[:, _sz, _sx].any():
                        continue
                    # Below-sea reject (oceans, deep lakes)
                    if col_desink[_sz, _sx] < 63:
                        _uwater_hit = True
                        break
                    # S71: river-water reject (carved channels above sea level).
                    # Need world coords to query water_col_mask.
                    if water_col_mask is not None:
                        tz_chk = local_z + _sz
                        tx_chk = local_x + _sx
                        if (0 <= tz_chk < tile_H and 0 <= tx_chk < tile_W
                                and water_col_mask[tz_chk, tx_chk]):
                            _uwater_hit = True
                            break
            if _uwater_hit:
                return
            if has_trunks:
                # Strategy A — trunk extension. Compute worst trunk gap.
                # S70-f5: reverted f4 — back to original (skip col_reject).
                max_trunk_gap = 0
                for _sz in range(sl):
                    for _sx in range(sw):
                        if not is_trunk_col[_sz, _sx] or col_reject[_sz, _sx]:
                            continue
                        ls = int(col_desink[_sz, _sx])
                        log_wy = place_y + int(lowest_log_sy_col[_sz, _sx])
                        gap = log_wy - ls
                        if gap > max_trunk_gap:
                            max_trunk_gap = gap
                if max_trunk_gap > MAX_TRUNK_EXT:
                    return  # too far to extend cleanly — reject
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
                max_gap = 0
                any_ground_col = False
                for _sz in range(sl):
                    for _sx in range(sw):
                        if col_reject[_sz, _sx] or not any_col[_sz, _sx]:
                            continue
                        lsy = int(lowest_sy_col[_sz, _sx])
                        if lsy > GROUND_COL_Y:
                            continue
                        ls = int(col_desink[_sz, _sx])
                        gap = (place_y + lsy) - ls
                        any_ground_col = True
                        if gap > max_gap:
                            max_gap = gap
                if any_ground_col and max_gap > PLACE_MAX_FLOAT_BUSH:
                    return
                if max_gap > 1:
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

        for sy in range(sh):
            world_y = place_y + sy
            yi = world_y - Y_MIN
            if yi < 0 or yi >= Y_RANGE:
                continue
            for sz in range(sl):
                tile_z = local_z + sz
                if tile_z < 0 or tile_z >= tile_H:
                    continue
                for sx in range(sw):
                    tile_x = local_x + sx
                    if tile_x < 0 or tile_x >= tile_W:
                        continue
                    # S61 column-level gates
                    if col_reject is not None:
                        if col_reject[sz, sx]:
                            continue
                        if world_y <= col_desink[sz, sx]:
                            continue
                    block_name = str(blk_arr[sy, sz, sx])
                    if "air" in block_name:
                        continue
                    bare = block_name.replace("minecraft:", "")
                    # Don't overwrite existing schematic blocks
                    existing_idx = vol[yi, tile_z, tile_x]
                    if existing_idx not in _OVERWRITABLE_CACHE:
                        existing_name = pal.name_of(existing_idx)
                        if any(k in existing_name for k in
                               ("log", "leaves", "fence", "planks",
                                "stairs", "slab", "vine")):
                            _PROTECTED_CACHE.add(existing_idx)
                        else:
                            _OVERWRITABLE_CACHE.add(existing_idx)
                    if existing_idx in _PROTECTED_CACHE:
                        continue
                    vol[yi, tile_z, tile_x] = pal.idx(bare)

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
                            col_log_name = str(blk_arr[_sy, _sz, _sx])
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
                    _SOFT_SOIL_NAMES = frozenset({
                        "dirt", "grass_block", "podzol", "coarse_dirt",
                        "rooted_dirt", "mud", "packed_mud", "moss_block",
                        "mycelium", "mud_block", "snow_block", "sand",
                        "red_sand",
                    })
                    for drop in range(1, ROOT_ANCHOR_DEPTH + 1):
                        fill_wy_down = ls - drop + 1  # ls is surface Y, so fill ls..ls-5
                        yi_down = fill_wy_down - Y_MIN
                        if not (0 <= yi_down < Y_RANGE):
                            continue
                        existing = vol[yi_down, tile_z, tile_x]
                        exist_name = pal.name_of(existing)
                        exist_bare = exist_name.replace("minecraft:", "").split("[")[0]
                        if exist_bare in _SOFT_SOIL_NAMES:
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


def _chunk_to_nbt_bytes(
    cx: int, cz: int,
    vol: np.ndarray,            # (Y_RANGE, tile_h, tile_w) uint16 palette indices
    pal: BlockPalette,          # palette for index → name conversion
    biome_grid: np.ndarray,     # (tile_h, tile_w) Vandir biome name strings
    tile_world_x: int, tile_world_z: int,
    tile_h: int, tile_w: int,
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

    # Convert to strings for NBT building (small: 512×16×16 = 131K cells)
    chunk_vol = pal.to_strings(chunk_u16)

    # Vandir biome (Z,X) grid for this chunk — default _DEFAULT for out-of-tile area
    biome_vandir_zx = np.full((CHUNK_SZ, CHUNK_SZ), "_DEFAULT", dtype=object)
    if bx_hi > bx_lo and bz_hi > bz_lo:
        biome_vandir_zx[lz_lo:lz_hi, lx_lo:lx_hi] = biome_grid[bz_lo:bz_hi, bx_lo:bx_hi]

    # S66/S67: altitude remap — pixels whose surface_y exceeds a threshold
    # and whose biome matches a remap's source get reassigned to the target
    # biome name for the MC tag emit.  S67 extends with a mountaincap dither
    # zone for SNOWY_BOREAL_TAIGA in Y[200, 250].
    if BIOME_ALTITUDE_REMAPS:
        _solid_for_remap = (chunk_vol != "air")
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

    # Ground MC biome per-cell (used for cells at or below surface)
    ground_q = np.vectorize(
        lambda b: BIOME_TO_MC.get(str(b), BIOME_TO_MC["_DEFAULT"])
    )(biome_vandir_q)  # (4, 4) MC biome strings

    # Sky MC biome per-cell (cells strictly above surface).  Falls back to
    # ground biome when Vandir biome not in BIOME_TO_MC_SKY.
    def _sky_of(b: str) -> str:
        return BIOME_TO_MC_SKY.get(str(b),
               BIOME_TO_MC.get(str(b), BIOME_TO_MC["_DEFAULT"]))
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
    solid_mask = (chunk_vol != "air")                 # (Y_RANGE, 16, 16)
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
    for sec_y in range(_SECTION_Y_MIN, _sec_y_max):
        yi_base = sec_y * CHUNK_SZ - Y_MIN   # vol Y-index of section's lowest block

        # Extract (16,16,16) block array in (Y,Z,X) order — fill with air
        sec_blk = np.full((CHUNK_SZ, CHUNK_SZ, CHUNK_SZ), "air", dtype=object)
        yi_lo = max(0, yi_base);  yi_hi = min(Y_RANGE, yi_base + CHUNK_SZ)
        by_lo = yi_lo - yi_base;  by_hi = yi_hi - yi_base
        # S84: track whether section is all-air (no copy needed if outside
        # vol range; if copied, check the copied data).
        _sec_populated = yi_hi > yi_lo and bx_hi > bx_lo and bz_hi > bz_lo
        if _sec_populated:
            sec_blk[by_lo:by_hi, lz_lo:lz_hi, lx_lo:lx_hi] = \
                chunk_vol[yi_lo:yi_hi, lz_lo:lz_hi, lx_lo:lx_hi]
            _is_all_air = bool((sec_blk == "air").all())
        else:
            _is_all_air = True  # section was outside vol range, sec_blk is the np.full default

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
        else:
            # Uniform-column fast path (original S60 behaviour)
            biome_q4 = np.stack([ground_q] * 4, axis=0)     # (4, 4, 4)

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
        # Biome compound still emitted per-section (biome can differ at
        # altitude vs ground, e.g. BOREAL_ALPINE sky override).
        _block_states_nbt = (
            _get_air_block_states_nbt() if _is_all_air
            else _build_block_states_nbt(sec_blk)
        )
        sections.append(nbtlib.Compound({
            "Y":            nbtlib.Byte(sec_y),
            "block_states": _block_states_nbt,
            "biomes":       _build_biomes_nbt(biome_q4),
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

    def _add_water_ticks(edge_vol, lx_fixed, lz_fixed):
        """edge_vol: (Y_RANGE, 16) slice. lx_fixed/lz_fixed: scalar or None (other axis varies).
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
            water_ys = np.where(col == "water")[0]
            if len(water_ys) == 0:
                continue
            yi = int(water_ys[-1])  # topmost water block only
            lx = lx_fixed if lx_fixed is not None else other
            lz = lz_fixed if lz_fixed is not None else other
            # Tile-local coords for river check.  cx,cz,tile_world_x/z and
            # river_water_y are all in scope from the enclosing write_tile.
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
        _add_water_ticks(chunk_vol[:, :, 0],  lx_fixed=0,  lz_fixed=None)
    if cx == cx1_tile:  # east edge — neighbour chunk (cx+1) is outside tile
        _add_water_ticks(chunk_vol[:, :, 15], lx_fixed=15, lz_fixed=None)
    if cz == cz0_tile:  # north edge — neighbour chunk (cz-1) is outside tile
        _add_water_ticks(chunk_vol[:, 0, :],  lx_fixed=None, lz_fixed=0)
    if cz == cz1_tile:  # south edge — neighbour chunk (cz+1) is outside tile
        _add_water_ticks(chunk_vol[:, 15, :], lx_fixed=None, lz_fixed=15)

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
        "Heightmaps":     _build_heightmaps_nbt(chunk_vol),
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
    for p in placements:
        local_x = p.world_x - tile_world_x
        local_z = p.world_z - tile_world_z
        if not (0 <= local_x < W and 0 <= local_z < H):
            continue  # outside tile bounds (edge overlap)
        try:
            schem_data = schem_loader.load_schem(Path(p.schem_path))
            schem_data._rotation = getattr(p, 'rotation', 0)
            stamp_schematic(vol, pal, schem_data, local_x, local_z,
                           p.place_y, surface_y=surface_y,
                           water_col_mask=water_col_mask)
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
