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
  Y_MAX    = 448   (build height)
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
Y_MAX     = 448
SEA_Y     = 63
SECTION_H = 16
Y_RANGE   = Y_MAX - Y_MIN          # 512
N_SECTIONS = Y_RANGE // SECTION_H  # 32

# MC 1.20.1 biome name → internal string (subset used in Vandir)
# Full MC biome IDs assigned by amulet via string name — no numeric IDs needed.
BIOME_TO_MC: dict[str, str] = {
    "COASTAL_HEATH":           "minecraft:plains",
    "TEMPERATE_RAINFOREST":    "minecraft:dark_forest",
    "BOREAL_TAIGA":            "minecraft:old_growth_birch_forest",
    "SNOWY_BOREAL_TAIGA":      "minecraft:snowy_taiga",
    "ALPINE_MEADOW":           "minecraft:meadow",
    "ARCTIC_TUNDRA":           "minecraft:frozen_peaks",
    "FROZEN_FLATS":            "minecraft:snowy_plains",
    "TEMPERATE_DECIDUOUS":     "minecraft:forest",
    "RAINFOREST_COAST":        "minecraft:old_growth_birch_forest",
    "RIPARIAN_WOODLAND":       "minecraft:dark_forest",
    "DRY_OAK_SAVANNA":         "minecraft:savanna",
    "KARST_BARRENS":           "minecraft:savanna_plateau",
    "BIRCH_FOREST":            "minecraft:birch_forest",
    "EASTERN_TEMPERATE_COAST": "minecraft:beach",
    "MIXED_FOREST":            "minecraft:forest",
    "CONTINENTAL_STEPPE":      "minecraft:plains",
    "DRY_PINE_BARRENS":        "minecraft:wooded_badlands",
    "SCRUBBY_HEATHLAND":       "minecraft:plains",
    "LUSH_RAINFOREST_COAST":   "minecraft:jungle",
    "SAND_DUNE_DESERT":        "minecraft:desert",
    "DESERT_STEPPE_TRANSITION":"minecraft:savanna_plateau",
    "SEMI_ARID_SHRUBLAND":     "minecraft:savanna",
    "DRY_WOODLAND_MAQUIS":     "minecraft:sparse_jungle",
    "TIDAL_JUNGLE_FRINGE":     "minecraft:sparse_jungle",
    "MANGROVE_COAST":          "minecraft:mangrove_swamp",
    "FRESHWATER_FEN":          "minecraft:swamp",
    # Fallback for ocean / unclassified
    "_OCEAN":                  "minecraft:ocean",
    "_DEFAULT":                "minecraft:plains",
}

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
    "ALPINE_MEADOW":          "andesite",
    "ARCTIC_TUNDRA":          "andesite",
    "BOREAL_TAIGA":           "andesite",
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
    wave_amp  = max(1, band_scale_y // 3)
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
) -> None:
    """Apply Y-banded fill to vol for columns matching col_mask, Y-sliced.

    ``col_y_noise`` is an optional (H, W) int array that shifts band boundaries
    per column by ±N blocks, producing organic-looking folded strata.

    ``band_lut`` is an optional precomputed Y → variant-index lookup table with
    randomised band thicknesses (from ``_build_band_lut``).  When provided,
    ``band_scale_y`` is ignored and the LUT is indexed directly.
    """
    lut_size = band_lut.shape[0] if band_lut is not None else 0
    SLICE = 32
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

        for vi, v_idx in enumerate(variant_indices):
            v_mask = cs_slice & (band_idx == vi)
            if v_mask.any():
                vol[y_s:y_e][v_mask] = v_idx


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
    stone_zone_top     = (surface_y - 4).astype(np.int32)     # (H, W)

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

    # 5b. Basement rock with lithology-palette banding
    #     Uses XZ waviness for natural-looking tilted strata, plus per-column
    #     Y noise for organic folded band edges.
    xz_waviness = _compute_xz_waviness(H, W, tile_world_x, tile_world_z, band_scale_y)

    # Per-column Y noise: ±3 blocks, deterministic from world position
    _noise_rng = np.random.default_rng(tile_world_x * 73856093 ^ tile_world_z * 19349669)
    col_y_noise = _noise_rng.integers(-3, 4, size=(H, W), dtype=np.int32)

    # Basement range: above bedrock band, below sediment bottom
    basement_mask = stone_mask & (abs_y > bedrock_band_top_y) & (abs_y < sed_bot_y[None, :, :])

    # Band thickness range (randomised per group for natural variation)
    _BAND_MIN = 4
    _BAND_MAX = 10
    _LUT_SIZE = Y_RANGE * 2  # enough headroom for waviness + noise offsets

    for gid, palette_idx_list in id_to_pal.items():
        group_cols = (lithology_tile == gid)  # (H, W)
        if not group_cols.any():
            continue
        # Deterministic seed per group + tile so bands differ between groups
        _lut_seed = tile_world_x * 73856093 ^ tile_world_z * 19349669 ^ gid * 2654435761
        band_lut = _build_band_lut(
            len(palette_idx_list), _BAND_MIN, _BAND_MAX, _LUT_SIZE, _lut_seed,
        )
        _apply_banded_fill(
            vol, basement_mask, group_cols, xz_waviness,
            palette_idx_list, len(palette_idx_list), band_scale_y,
            col_y_noise=col_y_noise,
            band_lut=band_lut,
        )

    # Columns with lithology_tile==0 (water/unclassified) keep stone (already filled)

    # 5c. Sediment layer: gravel / coarse_dirt / dirt by flow magnitude
    GRAVEL_IDX = pal.idx("gravel")
    COARSE_DIRT_IDX = pal.idx("coarse_dirt")
    DIRT_IDX = pal.idx("dirt")

    sed_mask = stone_mask & (abs_y >= sed_bot_y[None, :, :]) & (abs_y <= sed_top_y[None, :, :])

    if flow_tile is not None:
        high_flow_2d = (flow_tile > _SEDIMENT_FLOW_HIGH)
        med_flow_2d  = (flow_tile > _SEDIMENT_FLOW_MED) & ~high_flow_2d
        low_flow_2d  = ~high_flow_2d & ~med_flow_2d

        vol[sed_mask & high_flow_2d[None, :, :]] = GRAVEL_IDX
        vol[sed_mask & med_flow_2d[None, :, :]]  = COARSE_DIRT_IDX
        vol[sed_mask & low_flow_2d[None, :, :]]  = DIRT_IDX
    else:
        vol[sed_mask] = DIRT_IDX

    # 5d. Soil horizon: dirt on gentle slopes, coarse_dirt on steep
    soil_mask = stone_mask & (abs_y >= soil_bot_y[None, :, :]) & (abs_y <= soil_top_y[None, :, :])
    steep_2d = (slope_deg >= 18)  # moderate+ → coarse_dirt

    vol[soil_mask & ~steep_2d[None, :, :]] = DIRT_IDX
    vol[soil_mask & steep_2d[None, :, :]]  = COARSE_DIRT_IDX


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

    stone_mask  = (abs_y >= Y_MIN + 1) & (abs_y <= surf_broad - 4)
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

    # subsurface sy-2
    yi_sub2 = sy_flat - 2 - Y_MIN
    valid2 = (yi_sub2 >= 0) & (yi_sub2 < Y_RANGE)
    vol[yi_sub2[valid2], r_idx[valid2], c_idx[valid2]] = sub_idx_flat[valid2]

    # subsurface sy-3 — extra dirt buffer so geology never peeks at convex edges
    yi_sub3 = sy_flat - 3 - Y_MIN
    valid3 = (yi_sub3 >= 0) & (yi_sub3 < Y_RANGE)
    vol[yi_sub3[valid3], r_idx[valid3], c_idx[valid3]] = sub_idx_flat[valid3]

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
        _DOUBLE_TALL = frozenset({
            "tall_grass", "large_fern",
            "sunflower", "peony", "rose_bush", "lilac", "pitcher_plant",
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
    vol[water_mask] = WATER_IDX

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
    # Remove any grass-type ground_cover block (at sy+1) that has a water
    # source block directly below it (at sy).  Exempts lily_pad.
    # This catches grass/fern/flowers on bank pixels adjacent to water.
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
                # Check if the block AT surface_y (below the plant) is water
                if vol[sy_i, r, c] == WATER_IDX:
                    vol[cov_yi, r, c] = AIR_IDX
                    # Also clear upper half if double-tall
                    if cov_yi + 1 < Y_RANGE and vol[cov_yi + 1, r, c] in veg_indices:
                        vol[cov_yi + 1, r, c] = AIR_IDX

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
) -> None:
    """
    Stamp a schematic into the volume array.
    Accepts either:
      - SchemData dataclass (blocks = (Y,Z,X) ndarray of block name strings)
      - Legacy dict with blocks = list of (sx, sy, sz, color, block_name, props)
    Skips air blocks. Clips to tile bounds silently.
    Prevents leaf/fence/decorative blocks from being placed at or below surface_y.
    Log blocks CAN go below surface (roots/trunk base).
    """
    # Blocks that must never be underground
    _ABOVE_GROUND_ONLY = {"leaves", "fence", "sapling", "vine", "carpet",
                           "lantern", "slab"}
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

        # Use the PLACEMENT CENTER's surface_y as underground reference,
        # not each block's individual position.  On sloped terrain, edge
        # pixels have higher surface_y which culls low leaves on small trees.
        ref_surf = None
        if surface_y is not None:
            rz = min(max(local_z, 0), tile_H - 1)
            rx = min(max(local_x, 0), tile_W - 1)
            ref_surf = int(surface_y[rz, rx])

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
                    block_name = str(blk_arr[sy, sz, sx])
                    if "air" in block_name:
                        continue
                    bare = block_name.replace("minecraft:", "")
                    # Prevent leaves/fences/decorative from going underground
                    # Uses placement center's surface as reference (not per-pixel)
                    if ref_surf is not None and world_y <= ref_surf:
                        if "log" not in bare and "wood" not in bare:
                            continue
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


# ---------------------------------------------------------------------------
# 1.20.1 DIRECT REGION WRITER  (nbtlib + raw Anvil .mca — no amulet)
# ---------------------------------------------------------------------------
# Each 512×512-block tile maps exactly to one .mca region file.
# DataVersion 4556 = Java 1.21.10.  32 sections cover Y=-64 to Y=447.
# Block states and biomes use the 1.18+ padded long-array format.
# ---------------------------------------------------------------------------

import math      as _math
import io        as _io
import zlib      as _zlib
import struct    as _struct
import traceback as _traceback

_CHUNK_DATA_VERSION = 4556   # Java 1.21.10
_SECTION_Y_MIN      = -4     # Y_MIN // 16 = -64 // 16
_N_SECTIONS         = 32     # (448 - (-64)) // 16
_SECTOR_SZ          = 4096   # Anvil .mca sector size

_TEST_SECTION_Y_MAX = None   # Full Higher Heights range: sections -4 to 27 (Y -64 to 447)


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
        bpe     = 9
        vpl     = 64 // bpe                      # 7 values per long
        n_longs = _math.ceil(len(values) / vpl)  # 37
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
    biome_mc: np.ndarray,       # (tile_h, tile_w) MC biome strings (pre-translated)
    tile_world_x: int, tile_world_z: int,
    tile_h: int, tile_w: int,
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

    # Biome (Z,X) grid for this chunk — default plains for out-of-tile area
    biome_zx = np.full((CHUNK_SZ, CHUNK_SZ), "minecraft:plains", dtype=object)
    if bx_hi > bx_lo and bz_hi > bz_lo:
        biome_zx[lz_lo:lz_hi, lx_lo:lx_hi] = biome_mc[bz_lo:bz_hi, bx_lo:bx_hi]

    # Downsample to (4,4,4) biome quanta (Y,Z,X) — same biome at all Y levels
    biome_q4 = np.stack([biome_zx[::4, ::4]] * 4, axis=0)  # (4, 4, 4)

    sections = []
    _sec_y_max = (_TEST_SECTION_Y_MAX + 1) if _TEST_SECTION_Y_MAX is not None \
                 else (_SECTION_Y_MIN + _N_SECTIONS)
    for sec_y in range(_SECTION_Y_MIN, _sec_y_max):
        yi_base = sec_y * CHUNK_SZ - Y_MIN   # vol Y-index of section's lowest block

        # Extract (16,16,16) block array in (Y,Z,X) order — fill with air
        sec_blk = np.full((CHUNK_SZ, CHUNK_SZ, CHUNK_SZ), "air", dtype=object)
        yi_lo = max(0, yi_base);  yi_hi = min(Y_RANGE, yi_base + CHUNK_SZ)
        by_lo = yi_lo - yi_base;  by_hi = yi_hi - yi_base
        if yi_hi > yi_lo and bx_hi > bx_lo and bz_hi > bz_lo:
            sec_blk[by_lo:by_hi, lz_lo:lz_hi, lx_lo:lx_hi] = \
                chunk_vol[yi_lo:yi_hi, lz_lo:lz_hi, lx_lo:lx_hi]

        if np.all(sec_blk == "air"):
            continue   # omit fully-air sections to save file space

        sections.append(nbtlib.Compound({
            "Y":            nbtlib.Byte(sec_y),
            "block_states": _build_block_states_nbt(sec_blk),
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
        Only ticks the topmost water block per column — interior water is stable and never needs ticking."""
        for other in range(edge_vol.shape[1]):
            col = edge_vol[:, other]
            water_ys = np.where(col == "water")[0]
            if len(water_ys) == 0:
                continue
            yi = int(water_ys[-1])  # topmost water block only
            lx = lx_fixed if lx_fixed is not None else other
            lz = lz_fixed if lz_fixed is not None else other
            fluid_tick_list.append(nbtlib.Compound({
                "i": nbtlib.String("minecraft:water"),
                "t": nbtlib.Int(0),
                "p": nbtlib.Int(0),
                "x": nbtlib.Int(cx * CHUNK_SZ + lx),
                "y": nbtlib.Int(int(yi) + Y_MIN),
                "z": nbtlib.Int(cz * CHUNK_SZ + lz),
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
) -> list[str]:
    """
    Write the tile volume to a .mca region file.

    Each 512×512-block tile maps to exactly one region file (r.TX.TZ.mca).
    Uses nbtlib + direct Anvil format — no amulet dependency.
    Produces Java 1.21.10 chunks (DataVersion 4556) with 32 vertical sections.

    Returns list of written region file paths.
    """
    # Translate Vandir biome names → MC biome strings once for the whole tile
    biome_mc = np.vectorize(
        lambda b: BIOME_TO_MC.get(str(b), BIOME_TO_MC["_DEFAULT"])
    )(biome_grid)

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
                    cx, cz, vol, pal, biome_mc,
                    tile_world_x, tile_world_z, tile_h, tile_w,
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
    )

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
                           p.place_y, surface_y=surface_y)
        except Exception as e:
            import traceback
            print(f"  [warn] schematic stamp failed: {p.schem_path}: {e}",
                  flush=True)
            traceback.print_exc()

    # Step 3 — write to region files
    return write_tile_to_region(
        vol, pal, biome_grid,
        tile_world_x, tile_world_z,
        output_dir, H, W,
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
