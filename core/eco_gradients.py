"""
eco_gradients.py -- Ecological Gradient Computation
/core/eco_gradients.py

Derives ecological gradients from existing mask arrays for ecologically-
determined surface decoration.  Pure computation -- no I/O, no GUI.

Three consumers: column_generator (palette conditions), surface_decorator
(river banks, ground cover), and future schematic_placement (tree density).

All operations are vectorised numpy/scipy on (H, W) tiles.  Typical cost
~35-40 ms per 512x512 tile.
"""

from __future__ import annotations

import sys
from typing import NamedTuple, Optional

import numpy as np
from scipy.ndimage import laplace, distance_transform_edt


# ---------------------------------------------------------------------------
# Output container
# ---------------------------------------------------------------------------

class EcoGradients(NamedTuple):
    """Per-pixel ecological gradient fields, all float32 (H, W) in [0, 1]."""
    aspect:              np.ndarray   # radians [-pi, pi], compass facing
    north_factor:        np.ndarray   # 0 = south-facing (dry), 1 = north-facing (moist)
    concavity:           np.ndarray   # raw laplacian -- positive = basin, negative = ridge
    concavity_norm:      np.ndarray   # [0, 1] where 0 = most convex, 1 = most concave
    soil_depth:          np.ndarray   # [0, 1] proxy for organic layer / rooting depth
    moisture_index:      np.ndarray   # [0, 1] composite water availability
    wind_exposure:       np.ndarray   # [0, 1] exposure on ridges at elevation
    riparian_proximity:  np.ndarray   # [0, 1] closeness to flowing water (1 = at channel)
    lake_fringe:         np.ndarray   # [0, 1] closeness to lake edge (1 = at lake)
    gap_mask:            np.ndarray   # uint8: 0=none, 1=meadow, 2=windthrow, 4=floodplain, 5=rock, 6=alpine_meadow, 7=snow, 8=sand_dune
    rock_exposure_gradient: np.ndarray  # float32 [0,1] raw gradient for tree thinning
    rock_tight_gradient: np.ndarray  # float32 [0,1] tight rock gradient for dither
    snow_caps_gradient: np.ndarray   # float32 [0,1] raw snow cap gradient for dither
    sand_dunes_gradient: np.ndarray  # float32 [0,1] sand dune gradient for dither
    alpine_biome_source: np.ndarray  # (H, W) object str — nearest non-alpine biome name


# ---------------------------------------------------------------------------
# Blending primitives (used by consumers too)
# ---------------------------------------------------------------------------

def eco_sigmoid(value: np.ndarray, center: float, width: float) -> np.ndarray:
    """Smooth [0, 1] transition centred at *center* with *width* sharpness.

    width > 0: larger = gentler transition.
    Returns float32 array same shape as *value*.
    """
    # Clip exponent to avoid overflow in exp()
    x = np.clip(-(value - center) / max(width, 1e-6), -20.0, 20.0)
    return (1.0 / (1.0 + np.exp(x))).astype(np.float32)


def eco_power_blend(value: np.ndarray, exponent: float) -> np.ndarray:
    """Power-curve remap: exponent>1 concentrates near 0, <1 near 1."""
    return np.power(np.clip(value, 0.0, 1.0), exponent).astype(np.float32)


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "moisture_weights": {"flow": 0.4, "concavity": 0.3, "north_factor": 0.3},
    "soil_depth_slope_exponent": 1.5,
    "riparian_max_distance_px": 32,
    "lake_fringe_max_distance_px": 6,
}


def _get(cfg: dict, *keys, default=None):
    node = cfg
    for k in keys:
        if not isinstance(node, dict):
            return default
        node = node.get(k, default)
    return node


def compute_eco_gradients(
    surface_y:   np.ndarray,    # (H, W) int16 -- MC Y coordinates
    flow_f:      np.ndarray,    # (H, W) float32 [0, 1]
    erosion_f:   np.ndarray,    # (H, W) float32 [0, 1]
    cliff_deg:   np.ndarray,    # (H, W) float32 -- slope in degrees
    hydro_order: np.ndarray,    # (H, W) float32 [0, 1] (Strahler / 255)
    hydro_width: np.ndarray,    # (H, W) float32 [0, 1]
    hydro_lake:  np.ndarray,    # (H, W) float32 [0, 1] (lake ID / 65535)
    land_mask:   np.ndarray,    # (H, W) bool
    cfg:         dict,
    river_meta:  np.ndarray | None = None,  # (H, W) uint8 — actual water pixels
    tile_x:      int = 0,
    tile_z:      int = 0,
    biome_grid:  np.ndarray | None = None,  # (H, W) object — biome name strings
    hydro_floodplain: np.ndarray | None = None,  # (H, W) float32 [0,1] — precomputed mask
    wind_windthrow: np.ndarray | None = None,    # (H, W) float32 [0,1] — precomputed mask
    rock_exposure: np.ndarray | None = None,     # (H, W) float32 [0,1] — precomputed mask
    rock_exposure_tight: np.ndarray | None = None,  # (H, W) float32 [0,1] — tight rock peaks
    snow_caps: np.ndarray | None = None,            # (H, W) float32 [0,1] — snow cap mask
    sand_dunes: np.ndarray | None = None,           # (H, W) float32 [0,1] — sand dune mask
) -> EcoGradients:
    """Compute all ecological gradients for a single tile.

    Args:
        surface_y:   MC Y surface elevation (post-LUT, post-river-carve).
        flow_f:      Normalised flow accumulation.
        erosion_f:   Normalised erosion intensity.
        cliff_deg:   Spatial-gradient slope in degrees.
        hydro_order: Strahler stream order (0 = no channel).
        hydro_width: Channel width mask.
        hydro_lake:  Lake membership mask (0 = no lake).
        land_mask:   True where pixel is above sea level.
        cfg:         thresholds.json dict (reads ``eco_gradients`` section).

    Returns:
        EcoGradients namedtuple.
    """
    eco_cfg = _get(cfg, "eco_gradients", default=_DEFAULTS)

    H, W = surface_y.shape
    sy = surface_y.astype(np.float32)

    # ---- 1. Aspect (compass direction the slope faces) --------------------
    gy, gx = np.gradient(sy)
    aspect = np.arctan2(gy, gx).astype(np.float32)          # [-pi, pi]

    # North factor: cos(aspect) remapped so north=1, south=0
    # In image coords, negative gy = slope descends toward lower row = "north"
    # cos(aspect) is highest when aspect ~ 0 (east) which isn't quite right.
    # We want: north_factor high when slope faces north (toward -Y in image).
    # Slope faces direction of steepest DESCENT = (gx, gy).
    # "North" in MC world = -Z = -row direction.  Face-north = gy < 0.
    # Simple: north_factor = (-gy / magnitude) remapped to [0, 1].
    magnitude = np.hypot(gx, gy).clip(min=1e-6)
    north_factor = ((-gy / magnitude) + 1.0) / 2.0          # [0, 1]
    north_factor = north_factor.astype(np.float32)
    # Flat areas (no gradient) get 0.5 (neutral)
    flat = magnitude < 0.5
    north_factor[flat] = 0.5

    # ---- 2. Concavity (Laplacian of surface) ------------------------------
    concavity_raw = laplace(sy).astype(np.float32)
    # Positive = concave (basin), negative = convex (ridge)
    # Normalise to [0, 1] for downstream use, preserving sign in raw
    c_abs_max = max(np.abs(concavity_raw).max(), 1e-6)
    concavity_norm = ((concavity_raw / c_abs_max) + 1.0) / 2.0
    concavity_norm = concavity_norm.astype(np.float32)

    # ---- 3. Soil depth proxy ----------------------------------------------
    # Deep soil accumulates on flats and in basins; thin on steep slopes/ridges
    slope_exp = float(_get(eco_cfg, "soil_depth_slope_exponent", default=1.5))
    slope_factor = 1.0 - np.clip(cliff_deg / 90.0, 0.0, 1.0)
    slope_factor = np.power(slope_factor, slope_exp)         # flats >> gentle >> steep
    concavity_pos = np.clip(concavity_raw, 0.0, None)       # only basins contribute
    concavity_contrib = np.clip(concavity_pos / max(concavity_pos.max(), 1e-6),
                                0.0, 1.0)
    soil_depth = (0.6 * slope_factor + 0.4 * concavity_contrib).astype(np.float32)
    # Zero out ocean
    soil_depth[~land_mask] = 0.0

    # ---- 4. Moisture index ------------------------------------------------
    mw = _get(eco_cfg, "moisture_weights", default=_DEFAULTS["moisture_weights"])
    w_flow = float(mw.get("flow", 0.4))
    w_conc = float(mw.get("concavity", 0.3))
    w_nf   = float(mw.get("north_factor", 0.3))

    moisture_index = (
        w_flow * flow_f
        + w_conc * concavity_contrib
        + w_nf   * north_factor
    ).astype(np.float32)
    moisture_index = np.clip(moisture_index, 0.0, 1.0)
    moisture_index[~land_mask] = 0.0

    # ---- 5. Wind exposure -------------------------------------------------
    # High on convex ridges at elevation; low in basins and lowlands
    convexity = np.clip(-concavity_raw, 0.0, None)
    convexity_norm = np.clip(convexity / max(convexity.max(), 1e-6), 0.0, 1.0)
    sy_max = max(float(sy[land_mask].max()), 1.0) if land_mask.any() else 1.0
    elev_factor = np.clip(sy / sy_max, 0.0, 1.0)
    wind_exposure = (0.6 * convexity_norm + 0.4 * elev_factor).astype(np.float32)
    wind_exposure[~land_mask] = 0.0

    # ---- 6. Riparian proximity (distance to flowing water) ----------------
    rip_max_px = int(_get(eco_cfg, "riparian_max_distance_px", default=32))
    # Denormalise hydro_order: uint8 was /255, Strahler 0 = no channel
    hydro_order_u8 = np.round(hydro_order * 255.0).astype(np.uint8)
    has_channels = hydro_order_u8.max() > 0
    if has_channels:
        channel_mask = hydro_order_u8 > 0
        dist_from_channel = distance_transform_edt(~channel_mask).astype(np.float32)
        riparian_proximity = np.clip(1.0 - dist_from_channel / rip_max_px, 0.0, 1.0)
        riparian_proximity = riparian_proximity.astype(np.float32)
    else:
        riparian_proximity = np.zeros((H, W), dtype=np.float32)

    # ---- 7. Lake fringe (distance to actual lake water) -------------------
    lk_max_px = int(_get(eco_cfg, "lake_fringe_max_distance_px", default=6))
    # Use river_meta (actual carved water) when available — the hydro_lake
    # mask covers the entire basin, most of which is dry land with terrain
    # intersection.  river_meta == CHAN_LAKE marks only actual water + 2px bank.
    if river_meta is not None:
        lake_bool = (river_meta == 3)  # CHAN_LAKE
    else:
        hydro_lake_u16 = np.round(hydro_lake * 65535.0).astype(np.uint16)
        lake_bool = hydro_lake_u16 > 0
    has_lakes = lake_bool.any()
    if has_lakes:
        dist_from_lake = distance_transform_edt(~lake_bool).astype(np.float32)
        lake_fringe = np.clip(1.0 - dist_from_lake / lk_max_px, 0.0, 1.0)
        lake_fringe = lake_fringe.astype(np.float32)
    else:
        lake_fringe = np.zeros((H, W), dtype=np.float32)

    # ---- 8. Gap mask (forest clearings / windthrow / bare / floodplain) ----
    # Terrain-driven probability fields thresholded by world-space noise to
    # create discrete patches.  World-space coords ensure cross-tile continuity.
    #
    # Gap types:  1 = meadow clearing (wet basins, high moisture, low slope)
    #             2 = windthrow gap   (precomputed: exposed ridges, anisotropic)
    #             3 = bare patch      (thin soil, steep slopes, rock outcrop)
    #             4 = floodplain      (precomputed: river corridor, width-modulated)
    #             5 = rock exposure   (precomputed: above treeline + steep cliffs)
    #
    # Floodplain uses width-modulated corridor (not blob noise):
    #   dist_from_channel < local_floodplain_radius
    # where radius = base_from_strahler × (1-slope) × concavity_boost × noise.
    #
    # Priority: floodplain first (spatially constrained to rivers),
    #           then bare > windthrow > meadow (smallest features win ties).

    gap_mask = np.zeros((H, W), dtype=np.uint8)

    # Per-biome gap frequency targets (fraction of biome area)
    # Tuple order: (meadow, windthrow, bare, floodplain)
    _GAP_FREQ = {
        # biome:                  (meadow, windthrow, bare,  flood)
        "MIXED_FOREST":         (0.08, 0.04, 0.02, 0.25),
        "TEMPERATE_RAINFOREST": (0.03, 0.06, 0.01, 0.20),
        "BOREAL_TAIGA":         (0.05, 0.05, 0.03, 0.20),
        "TEMPERATE_DECIDUOUS":  (0.10, 0.03, 0.02, 0.25),
        "CONTINENTAL_STEPPE":   (0.15, 0.01, 0.05, 0.15),
        "DRY_PINE_BARRENS":     (0.05, 0.02, 0.04, 0.15),
        "DRY_OAK_SAVANNA":      (0.08, 0.01, 0.03, 0.10),
        "DRY_WOODLAND_MAQUIS":  (0.04, 0.01, 0.03, 0.10),
        "BIRCH_FOREST":         (0.08, 0.04, 0.02, 0.25),
        "MOSS_OLD_GROWTH":      (0.03, 0.05, 0.01, 0.15),
        "SUBTROPICAL_HUMID":    (0.05, 0.03, 0.01, 0.20),
        "TROPICAL_MONSOON_FOREST": (0.04, 0.02, 0.01, 0.15),
        "JUNGLE_HIGHLANDS":     (0.03, 0.02, 0.02, 0.10),
        "RAINFOREST_COAST":     (0.02, 0.04, 0.01, 0.10),
        "LUSH_RAINFOREST_COAST":(0.02, 0.04, 0.01, 0.10),
        "ALPINE_MEADOW":        (0.00, 0.00, 0.05, 0.00),
        "KARST_BARRENS":        (0.00, 0.00, 0.04, 0.00),
        "SCRUBBY_HEATHLAND":    (0.03, 0.01, 0.03, 0.08),
        "SAND_DUNE_DESERT":     (0.00, 0.00, 0.02, 0.00),
        "ARCTIC_TUNDRA":        (0.00, 0.00, 0.03, 0.00),
        "FROZEN_FLATS":         (0.00, 0.00, 0.02, 0.00),
        "SNOWY_BOREAL_TAIGA":   (0.02, 0.03, 0.02, 0.00),
    }
    _DEFAULT_FREQ = (0.03, 0.02, 0.02, 0.10)  # conservative fallback

    if land_mask.any():
        # -- Terrain probability fields --
        # Meadow: wet basins on flat ground
        slope_norm = np.clip(cliff_deg / 45.0, 0.0, 1.0)
        meadow_terrain = (
            0.4 * moisture_index
            + 0.4 * concavity_norm
            + 0.2 * (1.0 - slope_norm)
        )
        # (Bare patches removed — rock_exposure gradient handles alpine rock
        #  and cliff faces globally with proper treeline ecology.)

        # -- World-space simplex noise for patch shapes --
        # Two-octave noise: large scale sets patch regions, small scale
        # adds edge irregularity.  Gaussian blur on the result merges
        # nearby peaks into bigger coherent patches.
        try:
            import opensimplex as ox
            from scipy.ndimage import gaussian_filter as _gf_gap

            world_x0 = tile_x * W
            world_z0 = tile_z * H

            def _gap_noise(seed, scale_large, scale_small):
                """Two-octave noise → gaussian blur for coherent patches."""
                xs_l = (np.arange(W, dtype=np.float64) + world_x0) / scale_large
                zs_l = (np.arange(H, dtype=np.float64) + world_z0) / scale_large
                xs_s = (np.arange(W, dtype=np.float64) + world_x0) / scale_small
                zs_s = (np.arange(H, dtype=np.float64) + world_z0) / scale_small
                ox.seed(seed)
                n_large = ox.noise2array(xs_l, zs_l).astype(np.float32)
                ox.seed(seed + 1000)
                n_small = ox.noise2array(xs_s, zs_s).astype(np.float32)
                combined = 0.7 * n_large + 0.3 * n_small
                # Blur to merge nearby peaks into coherent patches
                blurred = _gf_gap(combined, sigma=6.0)
                return (blurred - blurred.min()) / max(blurred.max() - blurred.min(), 1e-6)

            # Meadow: large patches (scale 80/40, blur merges to ~20-30 radius)
            noise_meadow = _gap_noise(77001, 80.0, 40.0)

            _have_ox = True
        except ImportError:
            # Fallback: deterministic hash noise
            world_x0 = tile_x * W
            world_z0 = tile_z * H
            rows = np.arange(H) + world_z0
            cols = np.arange(W) + world_x0
            cc, rr = np.meshgrid(cols, rows)
            _hash = lambda s: (((cc.astype(np.int64)*73856093 + s) ^ (rr.astype(np.int64)*19349663 + s)) & 0xFFFFFFFF) / 4294967295.0
            noise_meadow = _hash(77001).astype(np.float32)
            _have_ox = False

        # -- Combined score: terrain × noise --
        meadow_score = meadow_terrain * noise_meadow

        # ── Floodplain corridor (from precomputed global mask) ─────────────
        # hydro_floodplain.tif is built by rebuild_floodplain.py at 1:8
        # globally — Strahler flood-fill, max-filter, slope gate, elevation
        # factor, coast decay, longitudinal noise.  Zero tile-boundary seams.
        # If the mask doesn't exist yet, floodplain is simply skipped.
        if hydro_floodplain is not None and hydro_floodplain.max() > 0:
            flood_precomputed = hydro_floodplain > 0.001  # uint8 0/1 via tile_streamer → 0 or 1/255
            # Exclude water and off-land pixels
            water_mask_fp = (river_meta > 0) if river_meta is not None else np.zeros((H, W), dtype=bool)
            flood_candidate = flood_precomputed & land_mask & ~water_mask_fp
            gap_mask[flood_candidate] = 4
        # ── End floodplain corridor ────────────────────────────────────────

        # ── Rock exposure gradient (three-zone from precomputed mask) ─────
        # PRIORITY: Rock exposure is applied BEFORE windthrow so that
        # above-treeline ridges get rock/alpine treatment, not windthrow.
        # Windthrow's TPI mask naturally lights up exposed ridges, but
        # those belong to the alpine zone, not the forest gap system.
        #
        # Three-layer alpine system:
        #   Layer 1 — Alpine meadow (gap==6): from rock_exposure gradient 0.3+
        #   Layer 2 — Rock (gap==5): from rock_exposure_tight mask (peaks only)
        #   Layer 3 — Snow (gap==7): from snow_caps mask (very tips)
        # Each layer overrides the previous.  Dither amplitude scales with
        # feature size: alpine 0.10, rock 0.07, snow 0.04.
        if rock_exposure is not None and rock_exposure.max() > 0.01:
            water_mask_re = (river_meta > 0) if river_meta is not None else np.zeros((H, W), dtype=bool)
            avail = land_mask & ~water_mask_re & (gap_mask == 0)

            # World-space noise for organic boundaries
            world_x0 = tile_x * W
            world_z0 = tile_z * H
            try:
                import opensimplex as _ox_re
                _xs1 = (np.arange(W, dtype=np.float64) + world_x0) / 6.0
                _zs1 = (np.arange(H, dtype=np.float64) + world_z0) / 6.0
                _ox_re.seed(88777)
                _n1 = _ox_re.noise2array(_xs1, _zs1).astype(np.float32)
                _xs2 = (np.arange(W, dtype=np.float64) + world_x0) / 24.0
                _zs2 = (np.arange(H, dtype=np.float64) + world_z0) / 24.0
                _ox_re.seed(88778)
                _n2 = _ox_re.noise2array(_xs2, _zs2).astype(np.float32)
                _jitter_base = (0.5 * _n1 + 0.5 * _n2)
                del _n1, _n2
            except ImportError:
                _rng_re = np.random.default_rng(world_x0 * 73856093 ^ world_z0 * 19349663)
                _jitter_base = (_rng_re.random((H, W)).astype(np.float32) - 0.5) * 2.0

            # Layer 1: Alpine meadow — from original gradient, 0.3+ (scaled dither)
            _jitter_alp = _jitter_base * 0.10
            re_jittered = rock_exposure + _jitter_alp
            gap_mask[avail & (re_jittered >= 0.3)] = 6
            del re_jittered, _jitter_alp

            # Layer 2: Rock — from tight mask. WIDE zone (low threshold)
            # so probabilistic dither in surface_decorator can fade rock
            # in/out across a broad band. Per Session 39 technique.
            if rock_exposure_tight is not None and rock_exposure_tight.max() > 0.01:
                _jitter_rock = _jitter_base * 0.12
                rt_jittered = rock_exposure_tight + _jitter_rock
                # Lowered from 0.4 → 0.15 — wider gap zone, dither handles edge
                gap_mask[avail & (rt_jittered >= 0.15)] = 5
                alpine_avail = land_mask & ~water_mask_re & (gap_mask == 6)
                gap_mask[alpine_avail & (rt_jittered >= 0.15)] = 5
                del rt_jittered, _jitter_rock, alpine_avail

            # Layer 3: Snow — from snow_caps mask. Subtle dither.
            # Overrides ALL other gap types (including floodplain) — at the
            # snow line, nothing else makes sense.
            if snow_caps is not None and snow_caps.max() > 0.01:
                _jitter_snow = _jitter_base * 0.05
                sc_jittered = snow_caps + _jitter_snow
                snow_avail = land_mask & ~water_mask_re
                gap_mask[snow_avail & (sc_jittered >= 0.4)] = 7
                del sc_jittered, _jitter_snow, snow_avail

            del _jitter_base

        # ── Sand dunes (gap==8) — desert basin sand fields ───────────────
        # INDEPENDENT of alpine block. Sand overrides floodplain/meadow/
        # windthrow (Class 2 staircase fix — those masks leak into desert
        # at high elevation flat plateaus, creating visible boundary).
        # Sand does NOT override alpine/rock/snow (high-elevation features).
        if sand_dunes is not None and sand_dunes.max() > 0.01:
            water_mask_sd = (river_meta > 0) if river_meta is not None else np.zeros((H, W), dtype=bool)
            # Override gap==0, 1, 2, 4 (regular biome, meadow, windthrow, floodplain)
            sd_overridable = (gap_mask == 0) | (gap_mask == 1) | (gap_mask == 2) | (gap_mask == 4)
            sd_avail = land_mask & ~water_mask_sd & sd_overridable
            # Local noise for sand boundary jitter
            _world_x0 = tile_x * W
            _world_z0 = tile_z * H
            try:
                import opensimplex as _ox_sd
                _sd_xs = (np.arange(W, dtype=np.float64) + _world_x0) / 18.0
                _sd_zs = (np.arange(H, dtype=np.float64) + _world_z0) / 18.0
                _ox_sd.seed(77003)
                _sd_noise = _ox_sd.noise2array(_sd_xs, _sd_zs).astype(np.float32) * 0.10
                del _sd_xs, _sd_zs
            except ImportError:
                _rng_sd = np.random.default_rng(_world_x0 * 73856093 ^ _world_z0 * 19349663)
                _sd_noise = (_rng_sd.random((H, W)).astype(np.float32) - 0.5) * 0.20
            sd_jittered = sand_dunes + _sd_noise
            # Wide gap zone (0.05 threshold) — probabilistic dither in
            # surface_decorator handles edge fade across the full range.
            gap_mask[sd_avail & (sd_jittered >= 0.05)] = 8
            del sd_jittered, _sd_noise, sd_avail, water_mask_sd

            # ── Post-pass: enforce monotonicity ──────────────────────────
            # snow(7) > rock(5) > alpine(6) — higher elevation wins
            from scipy.ndimage import maximum_filter as _mf_rock
            _rock = gap_mask == 5
            _meadow_alp = gap_mask == 6
            if _rock.any() and _meadow_alp.any():
                _rock_y = np.where(_rock, surface_y.astype(np.float32), -9999.0)
                _max_rock_y = _mf_rock(_rock_y, size=3)
                _invert = _meadow_alp & (surface_y.astype(np.float32) > _max_rock_y) & (_max_rock_y > -9999.0)
                gap_mask[_invert] = 5
                del _rock_y, _max_rock_y, _invert
            _snow = gap_mask == 7
            _rock2 = gap_mask == 5
            if _snow.any() and _rock2.any():
                _snow_y = np.where(_snow, surface_y.astype(np.float32), -9999.0)
                _max_snow_y = _mf_rock(_snow_y, size=3)
                _invert2 = _rock2 & (surface_y.astype(np.float32) > _max_snow_y) & (_max_snow_y > -9999.0)
                gap_mask[_invert2] = 7
                del _snow_y, _max_snow_y, _invert2
        # ── End rock/snow exposure ───────────────────────────────────────

        # ── Windthrow (from precomputed global mask) ──────────────────────
        # Applied AFTER rock exposure so above-treeline ridges stay rock/alpine.
        # wind_windthrow.tif is built by rebuild_windthrow.py at 1:8
        # globally — TPI + aspect + anisotropic noise.  Directional,
        # ridge-following swaths.  Zero tile-boundary seams.
        if wind_windthrow is not None and wind_windthrow.max() > 0:
            wt_precomputed = wind_windthrow > 0.001  # uint8 0/1 via tile_streamer → 0 or 1/255
            water_mask_wt = (river_meta > 0) if river_meta is not None else np.zeros((H, W), dtype=bool)
            wt_candidate = wt_precomputed & land_mask & ~water_mask_wt & (gap_mask == 0)
            gap_mask[wt_candidate] = 2
        # ── End windthrow ─────────────────────────────────────────────────

        # -- Per-biome meadow thresholding --
        # Floodplain + rock/alpine + windthrow already claimed their pixels.
        if biome_grid is not None:
            unique_biomes = np.unique(biome_grid)
            for biome in unique_biomes:
                bm = biome_grid == biome
                bm_land = bm & land_mask
                n_land = bm_land.sum()
                if n_land < 100:
                    continue

                freqs = _GAP_FREQ.get(str(biome), _DEFAULT_FREQ)
                freq_m = freqs[0]

                # Meadow clearings
                if freq_m > 0:
                    avail = bm_land & (gap_mask == 0)
                    if avail.sum() > 50:
                        scores_m = meadow_score[avail]
                        thr_m = np.percentile(scores_m, 100 * (1.0 - freq_m))
                        gap_mask[avail & (meadow_score >= thr_m)] = 1
        else:
            # No biome info — use default frequencies globally
            freqs = _DEFAULT_FREQ
            freq_m = freqs[0]
            avail = land_mask & (gap_mask == 0)
            if avail.sum() > 50 and freq_m > 0:
                scores_m = meadow_score[avail]
                thr_m = np.percentile(scores_m, 100 * (1.0 - freq_m))
                gap_mask[avail & (meadow_score >= thr_m)] = 1

        # Morphological closing on meadow patches — bridges thin gaps
        # caused by slope/contour lines splitting a single meadow in two.
        from scipy.ndimage import binary_closing as _bc_meadow
        meadow_raw = gap_mask == 1
        if meadow_raw.any():
            meadow_closed = _bc_meadow(meadow_raw, iterations=3)
            new_meadow = meadow_closed & (gap_mask == 0)
            gap_mask[new_meadow] = 1

        # Morphological closing on floodplain — more aggressive (iterations=5)
        # to bridge contour-line gaps and keep the corridor continuous.
        flood_raw = gap_mask == 4
        if flood_raw.any():
            flood_closed = _bc_meadow(flood_raw, iterations=5)
            new_flood = flood_closed & (gap_mask == 0)
            # Don't expand into water or off land
            water_mask_cl = (river_meta > 0) if river_meta is not None else np.zeros((H, W), dtype=bool)
            new_flood = new_flood & ~water_mask_cl & land_mask
            gap_mask[new_flood] = 4

        # Suppress gaps on water pixels
        water_mask = (river_meta > 0) if river_meta is not None else np.zeros((H, W), dtype=bool)
        gap_mask[water_mask] = 0
        gap_mask[~land_mask] = 0

        # Remove tiny fragments — minimum patch sizes for coherent clearings
        from scipy.ndimage import label as _label_gaps
        _MIN_PATCH = {1: 50, 2: 20, 4: 80, 5: 30, 6: 40}
        for gval, min_px in _MIN_PATCH.items():
            gmask = gap_mask == gval
            if not gmask.any():
                continue
            labeled, n_comp = _label_gaps(gmask)
            sizes = np.bincount(labeled.ravel())
            too_small = np.where(sizes < min_px)[0]
            if len(too_small) > 0:
                remove = np.isin(labeled, too_small)
                gap_mask[remove] = 0

    # Rock exposure gradient passthrough for tree thinning
    H, W = surface_y.shape
    re_grad = rock_exposure if rock_exposure is not None else np.zeros((H, W), dtype=np.float32)
    rt_grad = rock_exposure_tight if rock_exposure_tight is not None else np.zeros((H, W), dtype=np.float32)
    sc_grad = snow_caps if snow_caps is not None else np.zeros((H, W), dtype=np.float32)
    sd_grad = sand_dunes if sand_dunes is not None else np.zeros((H, W), dtype=np.float32)

    # Alpine biome source: for each alpine/rock/snow pixel AND for any
    # ALPINE_MEADOW biome pixel (regardless of gap), find the nearest
    # non-alpine biome name.  This lets MC biome AND surface palette inherit
    # from the surrounding biome — so a desert-region alpine pixel becomes
    # sand desert instead of grass meadow, eliminating the visible biome
    # boundary band between SAND_DUNE_DESERT and ALPINE_MEADOW.
    alpine_gap = (gap_mask == 5) | (gap_mask == 6) | (gap_mask == 7)
    alpine_biome = (biome_grid == "ALPINE_MEADOW") if biome_grid is not None else np.zeros((H, W), dtype=bool)
    alpine_any = alpine_gap | alpine_biome
    if biome_grid is not None and alpine_any.any():
        non_alpine = land_mask & ~alpine_any
        if non_alpine.any():
            from scipy.ndimage import distance_transform_edt as _edt_bio
            _, _idx_bio = _edt_bio(~non_alpine, return_indices=True)
            _alpine_bio_src = biome_grid[_idx_bio[0], _idx_bio[1]]
            del _idx_bio
        else:
            _alpine_bio_src = biome_grid.copy() if biome_grid is not None else np.full((H, W), "", dtype=object)
    else:
        _alpine_bio_src = biome_grid.copy() if biome_grid is not None else np.full((H, W), "", dtype=object)

    return EcoGradients(
        aspect=aspect,
        north_factor=north_factor,
        concavity=concavity_raw,
        concavity_norm=concavity_norm,
        soil_depth=soil_depth,
        moisture_index=moisture_index,
        wind_exposure=wind_exposure,
        riparian_proximity=riparian_proximity,
        lake_fringe=lake_fringe,
        gap_mask=gap_mask,
        rock_exposure_gradient=re_grad,
        rock_tight_gradient=rt_grad,
        snow_caps_gradient=sc_grad,
        sand_dunes_gradient=sd_grad,
        alpine_biome_source=_alpine_bio_src,
    )


# ---------------------------------------------------------------------------
# SMOKE TEST
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("eco_gradients.py -- smoke test")

    H, W = 512, 512
    rng = np.random.default_rng(42)

    # Synthetic terrain: a valley running diagonally with ridges on each side
    x = np.linspace(0, 4 * np.pi, W)
    z = np.linspace(0, 4 * np.pi, H)
    xx, zz = np.meshgrid(x, z)
    surface_y = (80 + 40 * np.sin(xx) * np.cos(zz * 0.5)).astype(np.int16)

    flow_f    = rng.random((H, W)).astype(np.float32) * 0.5
    erosion_f = rng.random((H, W)).astype(np.float32) * 0.3
    gy, gx    = np.gradient(surface_y.astype(np.float32))
    cliff_deg = np.degrees(np.arctan(np.hypot(gx, gy))).astype(np.float32)

    # Synthetic hydro: a single stream down the middle
    hydro_order = np.zeros((H, W), dtype=np.float32)
    hydro_order[:, W // 2 - 2 : W // 2 + 2] = 3.0 / 255.0  # Strahler 3
    hydro_width = np.zeros((H, W), dtype=np.float32)
    hydro_lake  = np.zeros((H, W), dtype=np.float32)
    # Small lake patch
    hydro_lake[100:130, 200:240] = 5.0 / 65535.0

    land_mask = surface_y >= 63
    cfg = {}

    eco = compute_eco_gradients(
        surface_y, flow_f, erosion_f, cliff_deg,
        hydro_order, hydro_width, hydro_lake,
        land_mask, cfg,
    )

    # Field checks
    assert eco.aspect.shape == (H, W), f"aspect shape: {eco.aspect.shape}"
    assert eco.north_factor.min() >= 0.0, f"north_factor min: {eco.north_factor.min()}"
    assert eco.north_factor.max() <= 1.0, f"north_factor max: {eco.north_factor.max()}"
    assert eco.soil_depth.min()  >= 0.0, f"soil_depth min: {eco.soil_depth.min()}"
    assert eco.soil_depth.max()  <= 1.0, f"soil_depth max: {eco.soil_depth.max()}"
    assert eco.moisture_index.min() >= 0.0
    assert eco.moisture_index.max() <= 1.0
    assert eco.wind_exposure.min()  >= 0.0
    assert eco.wind_exposure.max()  <= 1.0
    assert eco.riparian_proximity.min() >= 0.0
    assert eco.riparian_proximity.max() <= 1.0
    assert eco.lake_fringe.min() >= 0.0
    assert eco.lake_fringe.max() <= 1.0

    # Concavity should have both positive and negative values
    assert eco.concavity.min() < 0, "No convex terrain detected"
    assert eco.concavity.max() > 0, "No concave terrain detected"

    # Riparian proximity should be 1.0 at channel pixels
    channel_px = hydro_order > 0
    if channel_px.any():
        assert eco.riparian_proximity[channel_px].min() >= 0.99, \
            "Riparian not 1.0 at channel"

    # Lake fringe should be 1.0 inside lake
    lake_px = hydro_lake > 0
    if lake_px.any():
        assert eco.lake_fringe[lake_px].min() >= 0.99, \
            "Lake fringe not 1.0 inside lake"

    # Sigmoid / power blend checks
    test_arr = np.linspace(0, 1, 100).astype(np.float32)
    sig = eco_sigmoid(test_arr, center=0.5, width=0.1)
    assert sig.min() >= 0.0 and sig.max() <= 1.0
    assert sig[0] < 0.01 and sig[-1] > 0.99, "Sigmoid range issue"

    pw = eco_power_blend(test_arr, exponent=2.0)
    assert pw.min() >= 0.0 and pw.max() <= 1.0

    # Ocean pixels should be zeroed
    ocean = ~land_mask
    if ocean.any():
        assert eco.soil_depth[ocean].max() == 0.0
        assert eco.moisture_index[ocean].max() == 0.0

    print(f"  shape:              {H}x{W}")
    print(f"  north_factor range: [{eco.north_factor.min():.3f}, {eco.north_factor.max():.3f}]")
    print(f"  concavity range:    [{eco.concavity.min():.1f}, {eco.concavity.max():.1f}]")
    print(f"  soil_depth range:   [{eco.soil_depth.min():.3f}, {eco.soil_depth.max():.3f}]")
    print(f"  moisture_index:     [{eco.moisture_index.min():.3f}, {eco.moisture_index.max():.3f}]")
    print(f"  wind_exposure:      [{eco.wind_exposure.min():.3f}, {eco.wind_exposure.max():.3f}]")
    print(f"  riparian_prox:      [{eco.riparian_proximity.min():.3f}, {eco.riparian_proximity.max():.3f}]")
    print(f"  lake_fringe:        [{eco.lake_fringe.min():.3f}, {eco.lake_fringe.max():.3f}]")
    # Gap mask checks
    assert eco.gap_mask.shape == (H, W), f"gap_mask shape: {eco.gap_mask.shape}"
    assert eco.gap_mask.dtype == np.uint8
    assert eco.gap_mask.max() <= 4
    gap_counts = {i: int((eco.gap_mask == i).sum()) for i in range(4)}
    total_land = int(land_mask.sum())
    print(f"  gap_mask:           none={gap_counts[0]}, meadow={gap_counts[1]}, "
          f"windthrow={gap_counts[2]}, bare={gap_counts[3]}")
    if total_land > 0:
        pct = 100 * (total_land - gap_counts[0]) / total_land
        print(f"  total gap coverage: {pct:.1f}% of land")
    print(f"  sigmoid test:       PASS")
    print(f"  power_blend test:   PASS")
    print("PASS")
    sys.exit(0)
