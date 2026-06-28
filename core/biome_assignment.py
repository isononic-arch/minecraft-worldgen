"""
biome_assignment.py — Step 5: Biome Assignment Module
/core/biome_assignment.py

Assigns a biome string to every pixel in a 512×512 tile using a 4-stage pipeline.
All thresholds loaded from thresholds.json — never hardcoded.

Stage 0 — Override check (short-circuits if override_tile pixel != 0)
Stage 1 — Height + Slope → Terrain class
Stage 2 — Flow + Erosion → Hydrology override
Stage 3 — Flow (moisture proxy) + Altitude → Forest/Biome resolution

Height mask polarity (CORRECTED Session 13):
    HIGH value = HIGH terrain (mountains)
    LOW value  = LOW terrain (ocean/coast)
    Sea level threshold = 17050 (16-bit) → norm ≈ 0.260
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Union

import numpy as np


# ---------------------------------------------------------------------------
# OVERRIDE MAP  (override_tile pixel value → biome name)
# ---------------------------------------------------------------------------

OVERRIDE_BIOME_MAP: dict[int, str] = {
    0:   "",                       # no override
    10:  "COASTAL_HEATH",
    20:  "TEMPERATE_RAINFOREST",
    30:  "BOREAL_TAIGA",
    35:  "SNOWY_BOREAL_TAIGA",
    40:  "BOREAL_ALPINE",        # was ALPINE_MEADOW (S56→SNOWY_BOREAL_TAIGA, S58 first→BOREAL_TAIGA); S58 final → BOREAL_ALPINE: SNOWY_BOREAL_TAIGA's surface palette + non-snowy minecraft:taiga MC biome (no freeze, no precipitation snow)
    50:  "ARCTIC_TUNDRA",
    55:  "FROZEN_FLATS",
    60:  "TEMPERATE_DECIDUOUS",
    70:  "RAINFOREST_COAST",
    80:  "RIPARIAN_WOODLAND",
    90:  "DRY_OAK_SAVANNA",
    100: "KARST_BARRENS",
    110: "BIRCH_FOREST",
    115: "EASTERN_TEMPERATE_COAST",
    120: "MIXED_FOREST",
    130: "CONTINENTAL_STEPPE",
    140: "DRY_PINE_BARRENS",
    150: "SCRUBBY_HEATHLAND",
    160: "LUSH_RAINFOREST_COAST",
    170: "SAND_DUNE_DESERT",
    190: "DESERT_STEPPE_TRANSITION",
    200: "SEMI_ARID_SHRUBLAND",
    210: "DRY_WOODLAND_MAQUIS",
    220: "TIDAL_JUNGLE_FRINGE",
    230: "MANGROVE_COAST",
    240: "FRESHWATER_FEN",
    254: "_OCEAN",   # S95-T4: island-only open-ocean sentinel. Mainland override never emits 254; islands paint it on ~land so re-snapped sea-level cells short-circuit Stage 0 to _OCEAN instead of mis-classifying as coastal → SAND_DUNE_DESERT.
}

# Reverse map: biome name → override value
BIOME_OVERRIDE_MAP = {v: k for k, v in OVERRIDE_BIOME_MAP.items() if v}


# ---------------------------------------------------------------------------
# DEFAULT THRESHOLDS  (used if thresholds.json key is missing)
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "sea_level_16bit": 17050,
    "terrain_class": {
        "coastal_max_norm":  0.35,   # normalized [0,1] from 16-bit
        "lowland_max_norm":  0.55,
        "highland_max_norm": 0.72,
        "alpine_max_norm":   0.88,
        # above alpine_max → ice cap / frozen
        "slope_river_threshold": 0.08,
        "slope_cliff_threshold": 0.65,
    },
    "hydrology": {
        "flow_river_threshold":  0.70,
        "flow_wetland_threshold":0.45,
        "erosion_high":          0.60,
    },
    "moisture": {
        "flow_high":   0.55,
        "flow_mid":    0.30,
        "flow_low":    0.10,
    },
}


def _get(cfg: dict, *keys, default=None):
    """Nested dict get with default."""
    node = cfg
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k, default)
    return node


# ---------------------------------------------------------------------------
# STAGE HELPERS
# ---------------------------------------------------------------------------

def _terrain_class(height_norm: np.ndarray, cfg: dict) -> np.ndarray:
    """
    Returns string terrain class per pixel.
    Classes: "ocean", "coastal", "lowland", "highland", "alpine", "ice_cap"

    Height polarity (CORRECTED Session 13):
        HIGH raw 16-bit = HIGH terrain | LOW raw 16-bit = ocean floor
        height_norm = raw / 65535  →  0.0 = deepest ocean, 1.0 = highest peak
        sea level raw=17050 → sea_norm≈0.260
        ocean  = height_norm < sea_norm  (below sea level)
        land   = height_norm >= sea_norm (above sea level)
    """
    tc = _get(cfg, "terrain_class", default=_DEFAULTS["terrain_class"])
    t_height = height_norm   # no inversion — HIGH norm = HIGH terrain

    result = np.empty(height_norm.shape, dtype=object)
    # thresholds are fractions of the land range [sea_norm, 1.0]
    sea_norm   = _DEFAULTS["sea_level_16bit"] / 65535.0   # ~0.260
    land_range = 1.0 - sea_norm                            # ~0.740

    coastal_f  = _get(cfg, "terrain_class", "coastal_max_norm",  default=0.35)
    lowland_f  = _get(cfg, "terrain_class", "lowland_max_norm",  default=0.55)
    highland_f = _get(cfg, "terrain_class", "highland_max_norm", default=0.72)
    alpine_f   = _get(cfg, "terrain_class", "alpine_max_norm",   default=0.88)

    c_thr  = sea_norm + coastal_f  * land_range
    lo_thr = sea_norm + lowland_f  * land_range
    hi_thr = sea_norm + highland_f * land_range
    al_thr = sea_norm + alpine_f   * land_range

    result[:] = "ocean"
    land_mask  = t_height >= sea_norm
    result[land_mask & (t_height <  c_thr)]                  = "coastal"
    result[land_mask & (t_height >= c_thr)  & (t_height < lo_thr)] = "lowland"
    result[land_mask & (t_height >= lo_thr) & (t_height < hi_thr)] = "highland"
    result[land_mask & (t_height >= hi_thr) & (t_height < al_thr)] = "alpine"
    result[land_mask & (t_height >= al_thr)]                 = "ice_cap"

    return result


def _hydrology_override(flow: np.ndarray, erosion: np.ndarray,
                        terrain_class: np.ndarray, cfg: dict) -> np.ndarray:
    """
    Stage 2: apply flow/erosion-based biome overrides.
    Returns array of biome strings (empty string = no override).
    """
    result = np.full(flow.shape, "", dtype=object)

    flow_river   = _get(cfg, "hydrology", "flow_river_threshold",  default=0.70)
    flow_wetland = _get(cfg, "hydrology", "flow_wetland_threshold", default=0.45)

    land = terrain_class != "ocean"

    # High-flow lowland/coastal → Riparian Woodland
    riparian = land & (flow >= flow_river) & np.isin(terrain_class,
                       ["coastal", "lowland"])
    result[riparian] = "RIPARIAN_WOODLAND"

    # Mid-flow coastal → Freshwater Fen
    fen = land & (flow >= flow_wetland) & (flow < flow_river) & (
          terrain_class == "coastal")
    result[fen] = "FRESHWATER_FEN"

    return result


def _resolve_biome(terrain_class: np.ndarray, flow: np.ndarray,
                   height_norm: np.ndarray, cfg: dict) -> np.ndarray:
    """
    Stage 3: resolve final biome from terrain class + moisture (flow proxy).
    Returns array of biome name strings.
    """
    flow_high = _get(cfg, "moisture", "flow_high", default=0.55)
    flow_mid  = _get(cfg, "moisture", "flow_mid",  default=0.30)

    result = np.full(terrain_class.shape, "MIXED_FOREST", dtype=object)

    # Ocean
    result[terrain_class == "ocean"] = "_OCEAN"

    # Ice cap
    result[terrain_class == "ice_cap"] = "ARCTIC_TUNDRA"

    # Alpine — ALPINE_MEADOW retired S56; both flow-classes inherit SNOWY_BOREAL_TAIGA.
    # The override.tif EDT inheritance handles biome resolution at the map level;
    # this fallback only fires if assign_biomes() runs (MCA generation path).
    alpine = terrain_class == "alpine"
    result[alpine] = "SNOWY_BOREAL_TAIGA"

    # Highland
    high = terrain_class == "highland"
    result[high & (flow >= flow_high)] = "TEMPERATE_RAINFOREST"
    result[high & (flow >= flow_mid) & (flow < flow_high)] = "BOREAL_TAIGA"
    result[high & (flow <  flow_mid)] = "CONTINENTAL_STEPPE"

    # Lowland
    low = terrain_class == "lowland"
    result[low & (flow >= flow_high)] = "TEMPERATE_DECIDUOUS"
    result[low & (flow >= flow_mid) & (flow < flow_high)] = "MIXED_FOREST"
    result[low & (flow <  flow_mid)] = "DRY_OAK_SAVANNA"

    # Coastal
    coast = terrain_class == "coastal"
    result[coast & (flow >= flow_high)] = "RAINFOREST_COAST"
    result[coast & (flow >= flow_mid) & (flow < flow_high)] = "COASTAL_HEATH"
    result[coast & (flow <  flow_mid)] = "SAND_DUNE_DESERT"

    return result


# ---------------------------------------------------------------------------
# NOISE PATCH VARIATION  (Stage 3 Layer A)
# ---------------------------------------------------------------------------

_DOWNGRADE_RULES: dict[str, tuple[str, float]] = {
    # biome → (downgrade_to, noise_threshold)
    # Only applied at biome boundaries — dithers the transition edge,
    # never injects foreign biomes into uniform interiors.
    "TEMPERATE_DECIDUOUS": ("MIXED_FOREST",    0.72),
    "BOREAL_TAIGA":        ("MIXED_FOREST",    0.68),
    "MIXED_FOREST":        ("CONTINENTAL_STEPPE", 0.75),
    "TEMPERATE_RAINFOREST":("BOREAL_TAIGA",    0.70),
}

# Max distance from a biome boundary (in pixels) where dithering applies
_DITHER_BORDER_PX = 24


def _apply_biome_patch_noise(biome_grid: np.ndarray,
                              noise_fields: dict,
                              cfg: dict,
                              tile_x: int, tile_y: int) -> np.ndarray:
    """Apply fBm biome dithering at biome boundaries only.

    For each downgrade rule, only pixels within _DITHER_BORDER_PX of an
    actual boundary with a DIFFERENT biome are eligible.  This dithers
    transition edges without injecting foreign biomes into uniform interiors.
    Vectorized — no per-pixel Python loop.
    """
    if noise_fields is None:
        return biome_grid

    from scipy.ndimage import distance_transform_edt

    try:
        import opensimplex as ox
    except ImportError:
        return biome_grid

    patch_cfg = _get(cfg, "biome_patch_noise",
                     default={"scale": 300, "octaves": 4})
    scale   = float(patch_cfg.get("scale", 300))
    octaves = int(patch_cfg.get("octaves", 4))

    H, W = biome_grid.shape
    result = biome_grid.copy()

    world_x0 = tile_x * W
    world_z0 = tile_y * H

    # Find pixels near ANY biome boundary
    # A pixel is "near boundary" if within _DITHER_BORDER_PX of a pixel
    # belonging to a different biome.
    unique_biomes = np.unique(biome_grid)
    if len(unique_biomes) < 2:
        return biome_grid  # uniform tile — no boundaries to dither

    # Build boundary proximity: for each pixel, distance to nearest
    # pixel of a different biome
    same_biome = np.ones((H, W), dtype=bool)  # will be False at boundaries
    # Shift-compare to detect boundary pixels
    same_biome[:-1, :] &= (biome_grid[:-1, :] == biome_grid[1:, :])
    same_biome[1:, :]  &= (biome_grid[1:, :]  == biome_grid[:-1, :])
    same_biome[:, :-1] &= (biome_grid[:, :-1] == biome_grid[:, 1:])
    same_biome[:, 1:]  &= (biome_grid[:, 1:]  == biome_grid[:, :-1])
    boundary_px = ~same_biome
    if not boundary_px.any():
        return biome_grid

    dist_to_boundary = distance_transform_edt(~boundary_px).astype(np.float32)
    near_boundary = dist_to_boundary <= _DITHER_BORDER_PX

    # Generate noise field (vectorized)
    xs = (np.arange(W, dtype=np.float64) + world_x0) / scale
    zs = (np.arange(H, dtype=np.float64) + world_z0) / scale

    gen = noise_fields.get("biome_patch")
    base_seed = getattr(gen, '_seed', 55501) if gen is not None else 55501
    accumulated = np.zeros((H, W), dtype=np.float64)
    amplitude, freq, persistence, lacunarity = 1.0, 1.0, 0.5, 2.0
    for octave in range(octaves):
        ox.seed(base_seed + octave * 7919)
        accumulated += ox.noise2array(xs * freq, zs * freq) * amplitude
        amplitude *= persistence
        freq *= lacunarity
    # Normalize to [0, 1]
    lo, hi = accumulated.min(), accumulated.max()
    if hi - lo > 1e-9:
        noise_field = ((accumulated - lo) / (hi - lo)).astype(np.float32)
    else:
        noise_field = np.full((H, W), 0.5, dtype=np.float32)

    for biome_name, (downgrade_to, threshold) in _DOWNGRADE_RULES.items():
        # Only apply where: pixel IS this biome AND near a boundary
        eligible = (biome_grid == biome_name) & near_boundary
        if not eligible.any():
            continue
        # Noise exceeds threshold → dither to downgrade biome
        dither = eligible & (noise_field > threshold)
        if dither.any():
            result[dither] = downgrade_to

    return result


# ---------------------------------------------------------------------------
# BIOME BOUNDARY SOFTENING (S58)
# ---------------------------------------------------------------------------

_SOFTEN_BOUNDARY_GEN_CACHE: dict = {}


def _soften_gen_for_biome(biome_name: str):
    """OpenSimplex generator seeded deterministically per biome name.

    Independent generators per biome ensure each biome's boundary wobbles
    along its own organic curve — using the same generator for all biomes
    would shift everything in lockstep and produce no visible wobble.
    """
    cached = _SOFTEN_BOUNDARY_GEN_CACHE.get(biome_name)
    if cached is not None:
        return cached
    try:
        from opensimplex import OpenSimplex
    except ImportError:
        raise ImportError("opensimplex package required for biome boundary softening")
    seed = abs(hash(("vandir_biome_soften", biome_name))) & 0x7FFFFFFF
    gen = OpenSimplex(seed=seed)
    _SOFTEN_BOUNDARY_GEN_CACHE[biome_name] = gen
    return gen


def soften_biome_boundaries(
    biome_grid: np.ndarray,                # (H, W) object str
    px_off: int, py_off: int,              # world-space tile origin (for seamless noise)
    *,
    amplitude_px: float = 48.0,            # max boundary shift in blocks
    scale: float = 60.0,                   # noise wavelength in blocks
    octaves: int = 2,
    protect_zone_40: np.ndarray | None = None,  # (H, W) bool — alpine override pixels NOT to wobble
) -> np.ndarray:
    """Replace hard biome boundaries with wobbly organic curves.

    For each pair of adjacent biomes, compute distance-to-each via EDT and
    perturb each distance map with that biome's own world-seamless simplex
    noise field. Reassign each pixel near a boundary to the biome with the
    smallest perturbed distance. Pixels deep inside any biome's interior
    (further than ~amplitude_px from any boundary) keep their original biome.

    The result is a wide, organic transition zone where biome ASSIGNMENT
    itself is the wobble — not the per-pixel block dither. Each biome
    paints its own clean palette via the existing noise_layers_biome
    mechanism; the visible "softening" comes from where biomes meet.

    World-seamless: identical inputs at the same world coords produce
    identical biome assignments, so neighbouring tiles agree on shared
    boundary pixels (the cross-tile-ecotone padded path uses this too).

    Args:
        biome_grid:    (H, W) object array of biome name strings.
        px_off, py_off: World-space pixel origin of this grid's top-left
                       corner. For inner tile: tile_x * 512. For padded
                       inheritance grid: tile_x * 512 - inheritance_pad_px.
        amplitude_px:  How far a boundary can shift via noise. Boundaries
                       wobble within roughly ±amplitude_px of their original
                       position; the affected zone is ~2*amplitude_px wide.
        scale:         OpenSimplex wavelength. ~60 blocks gives organic
                       blob-shaped boundary curves.
        octaves:       fBm octave count.
        protect_zone_40: Optional mask of zone-40 pixels that should NOT
                       wobble — they got their biome via downslope alpine
                       inheritance and shouldn't be re-flipped by noise.

    Returns:
        (H, W) object array — modified copy of biome_grid.
    """
    from scipy.ndimage import distance_transform_edt as _edt

    H, W = biome_grid.shape
    biomes = [b for b in np.unique(biome_grid) if not str(b).startswith("_") and str(b)]
    if len(biomes) < 2 or amplitude_px <= 0:
        return biome_grid.copy()

    # Stack: perturbed_dists[i, r, c] = distance from (r,c) to biome[i]
    # plus an independent noise offset.
    #
    # S85: per-pixel uniform RNG noise replaces simplex noise.  Original
    # design used spatially-correlated simplex (scale=200 default) which
    # carved blob-shaped islands of one biome into another at the seam —
    # the wobbled distance maps had spatially-coherent regions where one
    # biome's perturbed distance was consistently lower, so whole patches
    # got reassigned together.  User reported visible blob structure at
    # FROZEN_FLATS boundaries in (33,6) walks.  Per-pixel uniform RNG has
    # NO spatial correlation, so reassignment becomes per-pixel salt-and-
    # pepper instead of blob-carving.  `scale` and `octaves` args are now
    # ignored (kept for back-compat).  `amplitude_px` controls the width
    # of the spray-paint zone — pixels within ~2*amplitude_px of a boundary
    # have nonzero swap probability; falloff is roughly quadratic-triangular
    # (50% right at the boundary, ~12.5% at amplitude_px in, ~0% at
    # 2*amplitude_px in).  World-seamless via splitmix64-style hash of
    # (biome_id, world_x, world_y) — adjacent tiles see identical noise
    # at shared boundary pixels.
    import zlib as _zlib
    perturbed = np.full((len(biomes), H, W), np.inf, dtype=np.float32)
    _xs_world = (np.arange(W, dtype=np.int64) + int(px_off)).astype(np.uint64)
    _ys_world = (np.arange(H, dtype=np.int64) + int(py_off)).astype(np.uint64)
    _xx, _yy = np.meshgrid(_xs_world, _ys_world)
    for i, b in enumerate(biomes):
        is_b = (biome_grid == b)
        if not is_b.any():
            continue
        dist_to_b = _edt(~is_b).astype(np.float32)  # 0 inside b, positive outside
        # splitmix64 hash: (biome_seed, world_x, world_y) -> uniform float32 in [0, 1)
        _biome_seed = np.uint64(_zlib.crc32(str(b).encode()))
        _h = _xx ^ (_yy * np.uint64(0x9E3779B97F4A7C15)) ^ _biome_seed
        _h = (_h * np.uint64(0xBF58476D1CE4E5B9))
        _h ^= _h >> 30
        _h = (_h * np.uint64(0x94D049BB133111EB))
        _h ^= _h >> 27
        noise_field = (_h.astype(np.float64) / np.float64(2**64)).astype(np.float32)
        # Remap [0,1] -> [-1,1] then scale to amplitude.
        noise_signed = (noise_field - 0.5) * 2.0
        perturbed[i] = dist_to_b + noise_signed * amplitude_px

    # For each pixel, pick the biome with the smallest perturbed distance.
    argmin_b = np.argmin(perturbed, axis=0)

    # Only REASSIGN pixels close to an original boundary. Pixels deep
    # inside their biome (own_depth > amplitude_px) keep their original
    # biome regardless of noise — prevents large interior blocks from
    # flipping just because their noise spike happened to align with
    # another biome's noise dip.
    own_depth = np.zeros((H, W), dtype=np.float32)
    for b in biomes:
        is_b = (biome_grid == b)
        if not is_b.any():
            continue
        own_depth_b = _edt(is_b).astype(np.float32)
        own_depth[is_b] = own_depth_b[is_b]
    near_boundary = own_depth <= amplitude_px

    out = biome_grid.copy()
    biomes_arr = np.array(biomes, dtype=object)
    new_assignment = biomes_arr[argmin_b]
    out[near_boundary] = new_assignment[near_boundary]

    # Restore protected zone-40 pixels (downslope-inherited) — they should
    # keep whatever the inheritance step decided, not be re-flipped by noise.
    if protect_zone_40 is not None:
        out[protect_zone_40] = biome_grid[protect_zone_40]

    return out


def ridge_watershed_override(
    biome_grid: np.ndarray,        # (H, W) object str
    height: np.ndarray,            # (H, W) float32 in [0, 1] (normalized terrain)
    *,
    altitude_threshold: float = 0.55,  # normalized; >0.55 ≈ MC Y > 140
    ridge_smoothing_sigma: float = 10.0,
    lowland_sample_band_px: int = 64,  # how far past the ridge to sample lowland biome
) -> np.ndarray:
    """Hard-override high-altitude pixels with a watershed-determined biome.

    Detects a 1D ridge axis (per-row column of max altitude, smoothed) and
    classifies each high-altitude pixel as WEST or EAST of the ridge.
    Auto-detects which lowland biome occupies each side by sampling the
    most-common non-high pixel within a horizontal band on that side.
    Each high-altitude pixel is then overwritten with the corresponding
    side's lowland biome.

    Effect: a clean watershed split for the alpine zone — west-face pixels
    inherit the windward lowland, east-face pixels inherit the leeward
    lowland. No EDT polygons, no downslope chaining; just a vertical
    cut at the ridge axis applied to everything above the altitude
    threshold.

    Args:
        biome_grid:           Source biome assignment (modified copy returned).
        height:               Normalized height [0,1]; HIGH = HIGH terrain.
        altitude_threshold:   Pixels with height > this are subject to override.
                              0.55 ≈ raw 36000 ≈ MC Y 140 per the terrain spline.
        ridge_smoothing_sigma: Gaussian sigma applied to ridge_col(row) to
                              smooth jagged per-row argmax noise.
        lowland_sample_band_px: Width of the horizontal sampling band on each
                              side of the ridge for biome auto-detection.

    Returns:
        (H, W) object array; high-altitude pixels reassigned to west-side
        or east-side lowland biome. If the tile has no high pixels or only
        one side has lowland samples, returns biome_grid unchanged.
    """
    H, W = biome_grid.shape
    high_mask = height > altitude_threshold
    if not high_mask.any():
        return biome_grid.copy()

    # ---- Phase 1: per-row ridge column = argmax(height) along row, smoothed.
    # If a row has no high pixels, ridge_col is undefined; we still get a
    # value (argmax of the lows) but skip override on rows with no high pixels.
    ridge_col_raw = np.argmax(height, axis=1).astype(np.float32)
    try:
        from scipy.ndimage import gaussian_filter1d
        ridge_col = gaussian_filter1d(ridge_col_raw, sigma=ridge_smoothing_sigma)
    except ImportError:
        ridge_col = ridge_col_raw
    ridge_col_int = np.clip(np.round(ridge_col).astype(np.int32), 0, W - 1)

    # ---- Phase 2: side classification.
    cols = np.arange(W, dtype=np.int32).reshape(1, -1)
    ridge_per_pixel = ridge_col_int.reshape(-1, 1)
    is_west = cols < ridge_per_pixel  # (H, W)
    is_east = cols > ridge_per_pixel  # ridge column itself excluded → no override there

    # ---- Phase 3: auto-detect lowland biome per side.
    low_mask = ~high_mask
    # Sample within band immediately west of ridge (covers most realistic cases)
    band_west = is_west & low_mask
    band_east = is_east & low_mask
    # Optionally further restrict by horizontal distance from ridge.
    if lowland_sample_band_px > 0:
        # distance from ridge in columns
        col_dist = np.abs(cols - ridge_per_pixel)  # (H, W)
        band_close = col_dist <= lowland_sample_band_px * 4  # 4x band for sampling robustness
        band_west = band_west & band_close
        band_east = band_east & band_close

    def _most_common_biome(mask):
        if not mask.any():
            return None
        from collections import Counter
        c = Counter(biome_grid[mask].tolist())
        # Filter out empty / underscore-prefixed biome names.
        for name, _n in c.most_common():
            if name and not str(name).startswith("_"):
                return name
        return None

    west_biome = _most_common_biome(band_west)
    east_biome = _most_common_biome(band_east)

    # ---- Phase 4: apply override to high-altitude pixels.
    out = biome_grid.copy()
    if west_biome is not None:
        out[high_mask & is_west] = west_biome
    if east_biome is not None:
        out[high_mask & is_east] = east_biome
    # Pixels exactly at ridge_col stay as their original biome (single-column
    # divider — visually negligible at 512×512, prevents an arbitrary tiebreak).

    return out


def _noise_tile_simple(gen, H: int, W: int,
                       px_off: int, py_off: int,
                       scale: float, octaves: int) -> np.ndarray:
    """Lightweight fBm tile generator (mirrors core/surface_decorator._noise_tile
    but locally-defined to avoid a cross-module import cycle).
    """
    try:
        import opensimplex as ox
    except ImportError:
        raise ImportError("opensimplex required for soften_biome_boundaries")

    xs = (np.arange(W, dtype=np.float64) + px_off) / scale
    ys = (np.arange(H, dtype=np.float64) + py_off) / scale
    base_seed = getattr(gen, '_seed', 42007)
    accumulated = np.zeros((H, W), dtype=np.float64)
    amp = 1.0
    freq = 1.0
    norm = 0.0
    for o in range(octaves):
        ox.seed(base_seed + o)
        accumulated += ox.noise2array(xs * freq, ys * freq) * amp
        norm += amp
        amp *= 0.5
        freq *= 2.0
    out = (accumulated / max(norm, 1e-9) + 1.0) / 2.0  # → [0, 1]
    return np.clip(out, 0.0, 1.0).astype(np.float32)


# ---------------------------------------------------------------------------
# MAIN ENTRY POINT
# ---------------------------------------------------------------------------

def assign_biomes(
    height_tile:   np.ndarray,
    slope_tile:    np.ndarray,
    flow_tile:     np.ndarray,
    erosion_tile:  np.ndarray,
    override_tile: np.ndarray,
    noise_fields:  dict,
    cfg:           dict,
    tile_x:        int = 0,
    tile_y:        int = 0,
) -> np.ndarray:
    """
    Assign biome strings to every pixel in the tile.

    Args:
        height_tile:   (H,W) float32 [0,1] — high=high terrain (corrected polarity)
        slope_tile:    (H,W) float32 [0,1]
        flow_tile:     (H,W) float32 [0,1] — moisture proxy
        erosion_tile:  (H,W) float32 [0,1]
        override_tile: (H,W) float32 [0,1] — maps to 8-bit zone values
        noise_fields:  dict of OpenSimplex generators
        cfg:           thresholds.json dict
        tile_x/y:      tile coordinates for noise world-space offset

    Returns:
        (H, W) object ndarray of biome name strings.
    """
    H, W = height_tile.shape

    # Convert override tile back to 8-bit zone values
    override_8bit = (override_tile * 255).round().astype(np.uint8)

    # Remove isolated zone specks — replace minority patches with dominant neighbour.
    # override.tif uses NEAREST upscale (8192→50k, ×6.1). Each source pixel →
    # ~6×6=36 output px. Jitter scatter at source boundaries creates isolated
    # small blobs of foreign zone codes (e.g. ARCTIC_TUNDRA at a coast boundary).
    # Strategy: label connected components per zone; any component smaller than
    # MIN_ZONE_PX gets flooded with the most common zone in its dilated halo.
    # Legitimate painted zones are many source-pixels wide → >>MIN_ZONE_PX, unaffected.
    try:
        import scipy.ndimage as _nd
        MIN_ZONE_PX = 400   # ~11×11 source-pixels at 6× scale; scatter blobs are ≤36px
        result_oz = override_8bit.copy()
        for zone_val in np.unique(override_8bit):
            if zone_val == 0:
                continue
            mask = override_8bit == zone_val
            labeled, n_comp = _nd.label(mask)
            if n_comp == 0:
                continue
            comp_sizes = _nd.sum(mask, labeled, range(1, n_comp + 1))
            for comp_idx, comp_size in enumerate(comp_sizes, 1):
                if comp_size >= MIN_ZONE_PX:
                    continue
                comp_mask = labeled == comp_idx
                # Dilate 8px to sample surrounding zone values
                halo = _nd.binary_dilation(comp_mask, iterations=8) & ~comp_mask
                surrounding = override_8bit[halo]
                if surrounding.size == 0:
                    continue
                vals, cnts = np.unique(surrounding, return_counts=True)
                replacement = int(vals[np.argmax(cnts)])
                result_oz[comp_mask] = replacement
        override_8bit = result_oz
    except ImportError:
        pass   # scipy unavailable — phantoms remain

    # Ocean protection: never apply land biome overrides to ocean pixels.
    # Source override PNGs may have land zones painted over ocean areas;
    # height_tile is the authoritative source for ocean vs land.
    # Any override pixel below sea level is zeroed out here so ocean pixels
    # always fall through to procedural assignment (→ _OCEAN).
    _sea_norm_thresh = _DEFAULTS["sea_level_16bit"] / 65535.0   # ≈ 0.260
    override_8bit[height_tile < _sea_norm_thresh] = 0

    # Stage 0: override check
    result = np.full((H, W), "", dtype=object)
    for zone_val, biome_name in OVERRIDE_BIOME_MAP.items():
        if zone_val == 0:
            continue
        result[override_8bit == zone_val] = biome_name

    no_override = result == ""

    # Stages 1–3 only for non-overridden pixels
    if no_override.any():
        tc = _terrain_class(height_tile, cfg)
        hydro = _hydrology_override(flow_tile, erosion_tile, tc, cfg)
        base  = _resolve_biome(tc, flow_tile, height_tile, cfg)

        # Apply hydrology override where set
        has_hydro = no_override & (hydro != "")
        result[has_hydro] = hydro[has_hydro]

        # Apply base biome where no hydro override
        no_hydro = no_override & (hydro == "")
        result[no_hydro] = base[no_hydro]

    # Stage 3 Layer A: noise patch variation
    result = _apply_biome_patch_noise(result, noise_fields, cfg, tile_x, tile_y)

    return result


# ---------------------------------------------------------------------------
# SMOKE TEST
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import numpy as np

    print("biome_assignment.py — smoke test")

    H, W = 64, 64
    rng = np.random.default_rng(42)

    height_tile   = rng.random((H, W)).astype(np.float32)
    slope_tile    = rng.random((H, W)).astype(np.float32) * 0.3
    flow_tile     = rng.random((H, W)).astype(np.float32)
    erosion_tile  = rng.random((H, W)).astype(np.float32) * 0.4
    override_tile = np.zeros((H, W), dtype=np.float32)

    # Paint some override zones
    override_tile[:10, :] = 120 / 255.0   # MIXED_FOREST
    override_tile[10:20, :] = 50 / 255.0  # ARCTIC_TUNDRA

    cfg = {}

    biome_grid = assign_biomes(
        height_tile, slope_tile, flow_tile, erosion_tile,
        override_tile, noise_fields=None, cfg=cfg,
    )

    assert biome_grid.shape == (H, W), f"Wrong shape: {biome_grid.shape}"
    assert biome_grid.dtype == object

    # Check overrides applied
    assert biome_grid[0, 0] == "MIXED_FOREST",  f"Override MIXED_FOREST failed: {biome_grid[0,0]}"
    assert biome_grid[10, 0] == "ARCTIC_TUNDRA", f"Override ARCTIC_TUNDRA failed: {biome_grid[10,0]}"

    # Check no empty strings remain
    empty = np.sum(biome_grid == "")
    assert empty == 0, f"{empty} pixels have empty biome"

    unique = np.unique(biome_grid).tolist()
    print(f"  shape:         {biome_grid.shape}")
    print(f"  unique biomes: {len(unique)}")
    print(f"  biomes:        {unique[:8]}...")
    print(f"  override check: MIXED_FOREST={biome_grid[0,0]} ✓")
    print(f"  override check: ARCTIC_TUNDRA={biome_grid[10,0]} ✓")
    print(f"  empty pixels:   {empty}")
    print("PASS")
    sys.exit(0)
