"""
run_pipeline.py — Step 10: CLI Entry Point
Vandir World Generation Pipeline — /pipeline/run_pipeline.py

Usage:
    python run_pipeline.py --config config/thresholds.json
                           --masks  C:/Users/nicho/masks/
                           --schem-index C:/Users/nicho/schematic_index.json
                           --output output/
                           [--threads N]
                           [--tile-x0 TX] [--tile-x1 TX]
                           [--tile-z0 TZ] [--tile-z1 TZ]
                           [--dry-run]

Outputs JSON IPC lines to stdout — one per event, flushed immediately.
All other logging goes to stderr.

IPC protocol (stdout only):
    {"type": "tile_start",    "tile_x": 4, "tile_y": 7}
    {"type": "tile_complete", "tile_x": 4, "tile_y": 7, "biomes": [...], "elapsed_ms": 4821}
    {"type": "tile_error",    "tile_x": 4, "tile_y": 7, "error": "..."}
    {"type": "pipeline_complete", "total_tiles": 9604, "elapsed_s": 7320}

Architecture rules (non-negotiable):
    - No PyQt6 / GUI imports anywhere in this file or /core/
    - No full raster loads — all mask reads via rasterio Window()
    - sys.stdout.flush() after every JSON line — no exceptions
    - Tile workers are independent processes (ProcessPoolExecutor)
    - Noise generators initialised once in the main process, seeds passed to workers
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# STDOUT IPC HELPERS — flush after every line, no exceptions
# ---------------------------------------------------------------------------

def _emit(obj: dict) -> None:
    print(json.dumps(obj))
    sys.stdout.flush()


def _log(msg: str) -> None:
    """Write to stderr only — never pollutes the IPC stdout channel."""
    print(f"[pipeline] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# WORLD / TILE GEOMETRY
# ---------------------------------------------------------------------------

WORLD_SIZE_PX   = 50_000        # pixels (= MC blocks at 1:1)
TILE_SIZE_PX    = 512           # pixels per tile side
TILES_PER_AXIS  = WORLD_SIZE_PX // TILE_SIZE_PX   # 97 (rounds down)
TOTAL_TILES     = TILES_PER_AXIS * TILES_PER_AXIS  # 9409


# ---------------------------------------------------------------------------
# TILE WORKER  (runs in subprocess — no shared state except args)
# ---------------------------------------------------------------------------

def _process_tile(args: dict) -> dict:
    """
    Full pipeline for one tile. Called in a worker process.

    args keys:
        tile_x, tile_y          int
        config_path             str
        masks_dir               str
        schem_index_path        str
        output_dir              str
        tile_size               int
        dry_run                 bool

    Returns dict with keys: tile_x, tile_y, biomes, elapsed_ms
    Raises on fatal tile error.
    """
    import importlib
    import sys as _sys
    from pathlib import Path as _Path

    # Ensure project root is on sys.path in the worker process (needed on Windows spawn)
    _project_root = str(_Path(__file__).resolve().parent)
    if _project_root not in _sys.path:
        _sys.path.insert(0, _project_root)

    # Dynamic imports — /core must be on sys.path (set by main before fork)
    core_biome_assign   = importlib.import_module("core.biome_assignment")
    core_tile_stream    = importlib.import_module("core.tile_streamer")
    core_col_gen        = importlib.import_module("core.column_generator")
    core_river          = importlib.import_module("core.river_carver_v2")
    core_decorator      = importlib.import_module("core.surface_decorator")
    core_placement      = importlib.import_module("core.schematic_placement")
    core_chunk          = importlib.import_module("core.chunk_writer")
    core_noise          = importlib.import_module("core.noise_fields")
    core_schem_loader   = importlib.import_module("core.schematic_loader")
    core_eco            = importlib.import_module("core.eco_gradients")
    core_clearing       = importlib.import_module("core.meadow_clearing_field")

    t0 = time.perf_counter()

    tile_x      = args["tile_x"]
    tile_y      = args["tile_y"]
    cfg_path    = _Path(args["config_path"])
    masks_dir   = _Path(args["masks_dir"])
    out_dir     = _Path(args["output_dir"])
    tile_sz     = args["tile_size"]
    dry_run     = args["dry_run"]

    # Load config
    with open(cfg_path) as f:
        cfg = json.load(f)

    # Pixel window for this tile
    col_off = tile_x * tile_sz
    row_off = tile_y * tile_sz
    # Clamp to world edge
    w = min(tile_sz, WORLD_SIZE_PX - col_off)
    h = min(tile_sz, WORLD_SIZE_PX - row_off)
    if w <= 0 or h <= 0:
        return {"tile_x": tile_x, "tile_y": tile_y, "biomes": [], "elapsed_ms": 0}

    # Noise generators — init per worker (cheap, deterministic)
    noise = core_noise.load_noise_generators(cfg_path)

    # ---- Step 4: Read mask tiles via tile_streamer ----
    # S60: build query-time gap config so rock_gap / snow_gap are sampled live
    # from the 8k Gaea sources via Catmull-Rom instead of the 50k TIFs.
    from core.gaea_gap_sampler import build_gap_config as _build_gap_cfg
    _gap_cfg = _build_gap_cfg(cfg.get("gaea_gaps", {}), masks_dir)
    masks = core_tile_stream.read_tile(
        masks_dir  = masks_dir,
        col_off    = col_off,
        row_off    = row_off,
        width      = w,
        height     = h,
        gap_config = _gap_cfg,
    )
    # masks dict keys: height, slope, erosion, flow, deposits, override, shore, river

    # ---- Step 4a-bis: apply painted hydro_region.png overlay ----
    # When masks/hydro_region.png exists with paint, this mutates the
    # hydro_* mask arrays in `masks` so the carver sees the painted
    # rivers instead of the on-disk WP-findPath centerline. Critical
    # for paint-as-sole-source mode — without this call, the legacy
    # staircased hydro_centerline.tif is what gets carved into the
    # world. Mirrors the same call in tools/_pipeline_runner.py:168.
    from core.hydro_region_overlay import apply_hydro_region_overlay
    apply_hydro_region_overlay(masks, masks_dir, col_off, row_off, w)

    # ---- Step 4a: Read discrete lithology mask (Phase 1.75) ----
    # lithology.tif is 6250×6250 (1:8 scale) — read at 1:8 coords, not full-res.
    # _fill_geology_layers() handles upscale 64→512 via NEAREST zoom.
    _lith_col = col_off // 8
    _lith_row = row_off // 8
    _lith_w   = max(1, w // 8)
    _lith_h   = max(1, h // 8)
    lithology_tile = core_tile_stream.read_discrete_tile(
        _Path(masks_dir) / "lithology.tif", _lith_col, _lith_row,
        width=_lith_w, height=_lith_h,
    )

    # ---- Step 5: Biome assignment ----
    biome_grid = core_biome_assign.assign_biomes(
        height_tile   = masks["height"],
        slope_tile    = masks["slope"],
        flow_tile     = masks["flow"],
        erosion_tile  = masks["erosion"],
        override_tile = masks["override"],
        noise_fields  = noise,
        cfg           = cfg,
    )

    unique_biomes = list(np.unique(biome_grid).tolist())

    # ---- Step 6: Column generation ----
    # generate_columns expects raw uint16 height, not normalised float [0,1]
    height_uint16 = np.round(masks["height"] * 65535.0).astype(np.uint16)
    surface_y = core_col_gen.generate_columns(
        height_tile  = height_uint16,
        slope_tile   = masks["slope"],
        biome_grid   = biome_grid,
        shore_tile   = masks["shore"],
        noise_fields = noise,
        cfg          = cfg,
        tile_x       = tile_x,
        tile_y       = tile_y,
    )

    # ---- Step 6a: River carving (v2 — precomputed hydrology masks) ----
    pre_carve_y = surface_y.copy()
    surface_y, river_meta, conn_channel_mask, water_y_field = core_river.carve_rivers(
        surface_y      = surface_y,
        flow_tile      = masks["flow"],
        river_tile     = masks["river"],
        cfg            = cfg,
        hydro_order    = masks.get("hydro_order"),
        hydro_width    = masks.get("hydro_width"),
        hydro_depth    = masks.get("hydro_depth"),
        hydro_lake     = masks.get("hydro_lake"),
        hydro_lkdep    = masks.get("hydro_lkdep"),
        hydro_lake_wl  = masks.get("hydro_lake_wl"),
        hydro_centerline = masks.get("hydro_centerline"),
        height_norm    = masks["height"],
        masks_dir      = masks_dir,
        tile_x         = tile_x,
        tile_z         = tile_y,
    )

    # S59: Scrap rivers in SAND_DUNE_DESERT — user opted out of wadi treatment.
    # Restore pre-carve terrain and zero river_meta on SDD pixels. Lakes
    # (river_meta == CHAN_LAKE == 3) are preserved per user's explicit ask.
    _sdd_river = (biome_grid == "SAND_DUNE_DESERT") & (river_meta != 3)
    if _sdd_river.any():
        surface_y[_sdd_river]  = pre_carve_y[_sdd_river]
        river_meta[_sdd_river] = 0

    # ---- Step 6b: Ecological gradients ----
    # Use Gaussian-smoothed cliff_deg (sigma=1.5) to eliminate 45° spikes at
    # every 1-block terrain step.  Raw np.gradient on integer surface_y treats
    # each staircase edge as a cliff, causing temperate_cliff_face / talus_apron
    # layers to paint stone contour-line bands across flat terrain.  S54 fix.
    cliff_deg = core_eco.compute_cliff_deg(surface_y)
    land_mask = surface_y >= core_col_gen.SEA_LEVEL

    eco_grads = core_eco.compute_eco_gradients(
        surface_y   = surface_y,
        flow_f      = masks["flow"],
        erosion_f   = masks["erosion"],
        cliff_deg   = cliff_deg,
        hydro_order = masks.get("hydro_order", np.zeros_like(masks["height"])),
        hydro_width = masks.get("hydro_width", np.zeros_like(masks["height"])),
        hydro_lake  = masks.get("hydro_lake",  np.zeros_like(masks["height"])),
        land_mask   = land_mask,
        cfg         = cfg,
        river_meta  = river_meta,
        tile_x      = tile_x,
        tile_z      = tile_y,
        biome_grid  = biome_grid,
        hydro_floodplain = masks.get("hydro_floodplain"),
        wind_windthrow = masks.get("wind_windthrow"),
        rock_gap = masks.get("rock_gap"),
        snow_gap = masks.get("snow_gap"),
        sand_dunes = masks.get("sand_dunes"),
        beach = masks.get("beach"),
        override_tile = masks.get("override"),
    )

    # ---- Step 6c: REMOVED S58 ----
    # Both the downslope alpine inheritance (v8, backup branch
    # backup/s58-v8-inheritance) and the ridge watershed override (v9,
    # produced "weird conflicts" in-game) are disabled. Alpine pixels
    # keep their assign_biomes default (SNOWY_BOREAL_TAIGA per
    # OVERRIDE_BIOME_MAP since S56). The soften+dither below handles
    # the visible biome-to-biome transition.

    # ---- Step 6c.5: Soften biome boundaries (S58) ----
    biome_grid = core_biome_assign.soften_biome_boundaries(
        biome_grid, tile_x * w, tile_y * h,
        amplitude_px=40.0, scale=200.0, octaves=2,
    )

    # ---- Step 6c2: Padded biome_grid for cross-tile ecotone (S58 Phase 3b) ----
    # Two different halo widths used here:
    #   INHERITANCE_PAD_PX=256 — wider context for downslope alpine inheritance,
    #       so each alpine pixel can follow the flow direction across up to a
    #       half-tile of neighbour terrain before hitting EDT fallback. Without
    #       this, each tile's inheritance sees only its own 512×512 window plus
    #       the narrow ecotone halo, and alpine plateaus larger than a tile
    #       produce per-tile-local decisions that disagree at seams → visible
    #       "square" seam artifacts.
    #   ECOTONE_PAD_PX=48 — narrower halo actually fed to _apply_ecotone_dither;
    #       this is a *softening width*, not a lookup range. Widening it would
    #       stretch the sigmoid in unhelpful ways.
    # Pipeline: read masks at the wider inheritance halo, run assign_biomes +
    # downslope inheritance on (512+2*256)² = 1024², overwrite the innermost
    # 512² with the authoritative inner biome_grid (post Step 6c inheritance),
    # then slice out the inner-plus-48 (608²) window as the dither's input.
    _INHERITANCE_PAD_PX = 512  # S58: full-tile context on each side
    _ECOTONE_PAD_PX = 48
    biome_grid_padded = None
    try:
        _padded_masks = core_tile_stream.read_tile(
            masks_dir   = masks_dir,
            col_off     = col_off,
            row_off     = row_off,
            width       = w,
            height      = h,
            pad_px      = _INHERITANCE_PAD_PX,
            mask_subset = ("height", "slope", "flow", "erosion", "override"),
        )
        _bg_big = core_biome_assign.assign_biomes(
            height_tile   = _padded_masks["height"],
            slope_tile    = _padded_masks["slope"],
            flow_tile     = _padded_masks["flow"],
            erosion_tile  = _padded_masks["erosion"],
            override_tile = _padded_masks["override"],
            noise_fields  = noise,
            cfg           = cfg,
        )
        # Padded boundary softening (cross-tile symmetric, no ridge override).
        _bg_big = core_biome_assign.soften_biome_boundaries(
            _bg_big,
            tile_x * w - _INHERITANCE_PAD_PX,
            tile_y * h - _INHERITANCE_PAD_PX,
            amplitude_px=40.0, scale=200.0, octaves=2,
        )
        # Overwrite the innermost 512×512 with the authoritative inner
        # biome_grid (which went through the Step 6c inner-scale inheritance
        # and matches the surface_blocks painted downstream).
        _bg_big[_INHERITANCE_PAD_PX:_INHERITANCE_PAD_PX + h,
                _INHERITANCE_PAD_PX:_INHERITANCE_PAD_PX + w] = biome_grid
        # Extract inner-plus-48 window for the ecotone dither.
        _lo = _INHERITANCE_PAD_PX - _ECOTONE_PAD_PX
        _hi_r = _INHERITANCE_PAD_PX + h + _ECOTONE_PAD_PX
        _hi_c = _INHERITANCE_PAD_PX + w + _ECOTONE_PAD_PX
        biome_grid_padded = _bg_big[_lo:_hi_r, _lo:_hi_c].copy()
        del _bg_big
    except Exception as _ecotone_pad_exc:  # noqa: BLE001
        # Non-fatal: fall back to unpadded ecotone dither.
        print(f"[ecotone_pad] WARN tile=({tile_x},{tile_y}): "
              f"{type(_ecotone_pad_exc).__name__}: {_ecotone_pad_exc}")
        biome_grid_padded = None

    # ---- Step 6d: Meadow clearing field (S57 Phase 3a) ----
    # Shared low-freq noise field read by both surface_decorator (ground cover
    # + surface block override in clearings) and schematic_placement (tree
    # density suppression).  Single field -> trees and grass clearings align
    # on the same seam deterministically.
    clearing_field = core_clearing.compute_meadow_clearing_field(
        tile_x, tile_y, H=surface_y.shape[0], W=surface_y.shape[1]
    )

    # ---- Step 7: Surface decoration ----
    _use_geo = bool(cfg.get("lithology", {}).get("feature_flag_enabled", False))
    _use_sp  = bool(cfg.get("surface_pipeline", {}).get("feature_flag_enabled", False))
    surface_blk, sub_blk, ground_cover = core_decorator.decorate_surface(
        surface_y    = surface_y,
        biome_grid   = biome_grid,
        erosion_tile = masks["erosion"],
        moisture_tile= masks["flow"],
        height_tile  = masks["height"],
        river_meta   = river_meta,
        flow_tile    = masks["flow"],
        noise_fields = noise,
        cfg          = cfg,
        tile_x       = tile_x,
        tile_y       = tile_y,
        eco_grads    = eco_grads,
        cliff_deg    = cliff_deg,
        use_new_geology = _use_geo,
        use_new_surface_pipeline = _use_sp,
        lithology_tile = lithology_tile if _use_sp else None,
        clearing_field = clearing_field,
        biome_grid_padded = biome_grid_padded,
    )

    # ---- Step 8: Schematic placement ----
    try:
        schem_index = core_placement.load_index(_Path(args["schem_index_path"]))
    except Exception:
        schem_index = {}

    placements = core_placement.place_schematics(
        surface_y    = surface_y,
        biome_grid   = biome_grid,
        river_meta   = river_meta,
        moisture_tile= masks["flow"],
        noise_fields = noise,
        cfg          = cfg,
        index        = schem_index,
        tile_x       = tile_x,
        tile_y       = tile_y,
        eco_grads    = eco_grads,
        cliff_deg    = cliff_deg,
        clearing_field = clearing_field,
        surface_blocks = surface_blk,
    )

    # ---- Step 9: Chunk write ----
    if not dry_run:
        # River water level — S72: per-pixel water_y_field from the WP
        # guardrails carve, NO legacy fallback.  If a footprint pixel
        # didn't get water_y_field set (factor > edge_threshold per WP
        # edge-water-skip rule), it stays dry land — matches JS exactly.
        # The legacy `pre_carve_y - 1` rule used in S71 created a per-pixel
        # checkerboard where edges followed terrain per-pixel while interiors
        # used the radius-average — visible jagged seam.  Lakes still get
        # their flat water level from the lake_water_levels pass below.
        carved = (river_meta > 0) & (surface_y < pre_carve_y)
        if water_y_field is not None:
            river_water_y = np.where(water_y_field > 0,
                                     water_y_field,
                                     np.int16(-999)).astype(np.int16)
        else:
            river_water_y = np.where(carved, pre_carve_y - 1, np.int16(-999)).astype(np.int16)

        # Flatten lake water to a constant Y per connected lake body.
        # Use ALL lake pixels for labeling (not just carved ones) — shallow
        # edge pixels with 0 carve depth would fragment the lake into
        # concentric rings, each getting a different water level → staircase.
        CHAN_LAKE = np.uint8(3)
        CHAN_RIVER = np.uint8(2)
        lake_mask = river_meta == CHAN_LAKE
        if lake_mask.any():
            from scipy.ndimage import label as _label_lakes
            from scipy.ndimage import distance_transform_edt as _edt_lakes
            lake_labeled, n_lakes = _label_lakes(lake_mask)
            lake_water_levels = np.full(n_lakes + 1, -999, dtype=np.int16)
            # S80 v26 — per-component MIN ceil lake water level:
            #
            # CLAUDE.md hard rule: "Shoreline = terrain intersection
            # (height < spill_elevation). NEVER morph/blur/spline/gaussian
            # on hydro_lake mask."
            #
            # FAIL HISTORY:
            #   v23: median + ceil + force surface_y down at every lake cell
            #         → carved basin walls, looked like spillover.
            #   v25: per-pixel ceil(water_y_float) per cell, no force-down
            #         → hydro_lake_wl varies 1-2 blocks within one connected
            #           component → STEPPED water surface → spillover at
            #           the step boundary.
            #
            # v26 fix: ONE flat Y per terrain-intersection connected
            # component, computed as MIN(ceil(water_y_float)) across the
            # component. This is the LOWEST spill point in the basin
            # cluster — guarantees water never sits above any cell whose
            # terrain rises above that Y. No force-down. Cells where
            # terrain int >= component_water_y naturally show no water
            # (chunk_writer's `abs_y > surface_y & abs_y <= rw` is empty
            # there) — which gives the correct natural shoreline.
            _hydro_lake_wl = masks.get("hydro_lake_wl")
            if _hydro_lake_wl is not None:
                _gaea_in = np.array([0, 17050, 45000, 65496], dtype=np.float64)
                _mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
                _wl_mc_float = np.interp(
                    (_hydro_lake_wl * 65535.0).ravel(),
                    _gaea_in, _mc_y_out
                ).reshape(_hydro_lake_wl.shape).astype(np.float32)
            else:
                _wl_mc_float = None

            for lid in range(1, n_lakes + 1):
                lk = lake_labeled == lid
                if not lk.any():
                    continue
                if _wl_mc_float is not None:
                    _wl_vals = _wl_mc_float[lk]
                    _wl_vals = _wl_vals[_wl_vals > -64]
                    if len(_wl_vals):
                        # MIN of floor'd water_y across the entire
                        # terrain-intersection component. This is the
                        # lowest spill elevation in the cluster.
                        # floor (not ceil): wl_mc=63.4 means the water
                        # surface sits between Y63 and Y64. Filling water
                        # blocks up to Y64 (ceil) puts a water block at
                        # Y64 which is ONE BLOCK ABOVE any rim cell with
                        # terrain at Y63 → user sees water spilling onto
                        # the bank. floor(63.4)=63 keeps water flush with
                        # the basin rim.
                        lake_water = int(np.floor(float(_wl_vals.min())))
                    else:
                        lake_water = int(pre_carve_y[lk].min()) + 1
                else:
                    lake_water = int(pre_carve_y[lk].min()) + 1
                # S81 v8.10 Option A REVERTED — caused regression where
                # painted-river cells inside precompute basins got tagged
                # CHAN_LAKE by the carver, then capped to low water_y by
                # the rim formula → dry. v8.11 will need a paint-aware
                # version that distinguishes painted-river cells from
                # painted-lake cells before capping.
                lake_water_levels[lid] = np.int16(lake_water)
                river_water_y[lk] = np.int16(lake_water)

            # Connectivity channels: ensure continuous water end-to-end.
            # Each channel connects a lake to a river (or river to river
            # via a lake).  Water level = shallowest endpoint so water
            # flows naturally downhill through the channel.
            CHAN_STREAM = np.uint8(1)
            # S80: connectivity-channel post-process REMOVED.  WP findPath
            # in hydrology_precompute produces guaranteed-connected paths
            # (mountain → lake / ocean, spillpoint → next sink), so the
            # carver's connectivity layer is empty by construction
            # (conn_channel_mask is always empty).  All the lake/river
            # endpoint-finding + water-level blending here was a no-op
            # whenever connectivity was off and is now permanently dead.

            # Blend river water level toward lake level at river-lake interfaces.
            # River pixels near a lake adopt the lake's flat water Y, tapering
            # back to per-pixel terrain-following over BLEND_DIST blocks.
            #
            # S80: include CHAN_STREAM in the blend zone.  Previously this
            # was gated on CHAN_RIVER (Strahler order >= 3), which never
            # matched WP findPath output (all paths emit as order=1 →
            # CHAN_STREAM).  Result before fix: river→lake water Y had a
            # visible step (river-water-Y vs lake_wl), creating the
            # connection gap the user observed at (51,53).
            #
            # S81 v8.12: BLEND_DIST 8 → 24. The 8-cell zone was too short
            # to bridge large elevation gaps (e.g. precompute basin at
            # Y=89 with painted-river inlet at terrain Y=70 = 19-block
            # gap). Beyond the 8-cell zone, river_water_y dropped back to
            # the river formula (~Y=70) — visually disconnected from the
            # lake at Y=89. With BLEND_DIST=24, the blend zone covers a
            # full 19-block elevation gap with ~1.25 blocks/cell falloff,
            # producing a smooth visible cascade from lake elevation
            # down to natural river elevation. The escape-fix + EDT berm
            # automatically handle bank containment for the now-higher
            # river_water_y in the blend zone.
            BLEND_DIST = 24
            river_carved = ((river_meta == CHAN_RIVER) | (river_meta == CHAN_STREAM)) & carved
            if river_carved.any():
                dist_from_lake = _edt_lakes(~lake_mask)
                blend_zone = river_carved & (dist_from_lake <= BLEND_DIST)
                if blend_zone.any():
                    from scipy.ndimage import maximum_filter as _maxf
                    expanded_labels = _maxf(lake_labeled, size=2 * BLEND_DIST + 1)
                    blend_lids = expanded_labels[blend_zone]
                    t = dist_from_lake[blend_zone].astype(np.float32) / BLEND_DIST
                    lake_y = lake_water_levels[blend_lids].astype(np.float32)
                    river_y = river_water_y[blend_zone].astype(np.float32)
                    blended = np.round(lake_y * (1.0 - t) + river_y * t).astype(np.int16)
                    river_water_y[blend_zone] = blended

        # S80: S78 wall-to-wall post-process REMOVED.  Was a no-op once
        # connectivity was disabled (S77+) and stays a no-op now that the
        # connectivity layer has been deleted entirely.

        # S81 v8.6: WP-style "fix water escaping" pass (river_script1.7
        # lines 688-737). Replaces the v8.5 lake wall with a more general
        # iterative pass that handles lake containment, river bank leaks,
        # AND river-lake junction leaks in one consistent invariant.
        #
        # Principle: at convergence, no water cell has a neighbor with
        # surface_y < water_y. If a neighbor's terrain dips below the
        # water level, MC fluid physics would activate at that cell and
        # cascade water down to it. Raising the neighbor's terrain to the
        # water level (or higher) blocks the cascade.
        #
        # Implementation:
        #   For each cell with water_y > 0 (river or lake water):
        #     Get max water_y in a 3x3 neighborhood
        #     If ANY cell in tile has surface_y < that max water_y AND
        #        is not itself a water cell at that level, raise its
        #        surface_y.
        #   Iterate up to N times until no further changes (typically
        #   converges in 2-3 iterations).
        #
        # Edge cases handled by the iterative form:
        #   - Lake wall thickness scales naturally with how far terrain
        #     dips. A 3-block-thick wall isn't hardcoded; it's whatever
        #     it takes to contain the water.
        #   - River bank leaks: bank-smooth pass may have dropped a bank
        #     cell below water; this pass raises it back.
        #   - River-lake junction: same containment invariant applies
        #     uniformly — no special case needed.
        from scipy.ndimage import maximum_filter as _maxf_escape
        from scipy.ndimage import distance_transform_edt as _edt_berm
        # Treat -999 (no water) as 0 so it doesn't dominate the max.
        _water_y_positive = np.where(
            river_water_y > 0, river_water_y, np.int16(0)
        ).astype(np.int16)
        # S81 v8.8 fix for "lake terrace blocks flow at junction":
        # The escape-fix only raises LAND cells (river_water_y < 0).
        # Water cells with a lower water_y than a neighbor's are LEFT
        # ALONE — MC fluid physics will cascade water between them
        # naturally (lake water spills down into river channel,
        # contained by the river banks which the escape-fix already
        # raised). Previously the condition also raised water cells,
        # creating a dam at the lake-river junction that prevented
        # flow.
        for _escape_iter in range(5):
            _nbr_max_wy = _maxf_escape(_water_y_positive, size=3)
            _leak_cells = (
                (surface_y < _nbr_max_wy)
                & (_nbr_max_wy > core_col_gen.SEA_LEVEL)
                & (river_water_y < 0)  # land only
            )
            if not _leak_cells.any():
                break
            surface_y[_leak_cells] = _nbr_max_wy[_leak_cells]

        # S81 v8.8: EDT-based smooth-slope berm. (v8.9 shift reverted —
        # `dist - 1` formula caused a major regression: rivers in the
        # middle of the 3×3 didn't connect to the lake and looked
        # missing. Reverted to v8.8's `dist` formula.) Slope:
        #   dist=1 → water_y - 1
        #   dist=2 → water_y - 2
        #   ...
        #   dist=8 → water_y - 8
        # The escape-fix above already pinned dist=1 to water_y; this
        # berm only raises cells beyond that.
        BERM_RADIUS = 8
        _water_mask_for_berm = (river_water_y > core_col_gen.SEA_LEVEL)
        if _water_mask_for_berm.any():
            _dist_from_water, _water_idx = _edt_berm(
                ~_water_mask_for_berm, return_indices=True
            )
            _nearest_water_y = river_water_y[_water_idx[0], _water_idx[1]]
            _berm_target = (
                _nearest_water_y.astype(np.int16)
                - _dist_from_water.astype(np.int16)
            )
            _need_berm = (
                (surface_y < _berm_target)
                & (_berm_target > core_col_gen.SEA_LEVEL)
                & (river_water_y < 0)
                & (_dist_from_water <= BERM_RADIUS)
            )
            if _need_berm.any():
                surface_y[_need_berm] = _berm_target[_need_berm]

        # S81 v8.14: FINAL WATER-LEVEL CLEANUP.
        # Rule (user-requested final pass): in rivers, water_y must NEVER
        # be at or above the adjacent NON-CARVED (pre_carve_y) bank
        # elevation. The v8.12 BLEND raises river_water_y near lakes so
        # the cascade is visually connected — but the raise can put
        # river water_y AT or ABOVE the natural bank, making escape-fix
        # raise the bank artificially (the "1-cell wall sticking up out
        # of the land" look). This pass lowers river_water_y back below
        # natural bank everywhere it would spill.
        #
        # Implementation:
        #   1. min_bank_3x3 = minimum_filter of pre_carve_y over a 3x3
        #      neighborhood, masking out river cells with HIGH so they
        #      don't contribute to the min. Gives the lowest natural
        #      adjacent bank for each cell.
        #   2. For interior cells of wide rivers (no bank in 3x3),
        #      propagate the cap from the nearest edge cell via EDT.
        #      Wide rivers get a uniform cap per cross-section.
        #   3. Lower river_water_y to (min_bank - 1) where currently
        #      above.
        #
        # Trade-off: the BLEND's smooth lake→river cascade collapses
        # into a sharp visible drop at the lake-river junction (since
        # river water_y is hard-capped at bank-1, not allowed to gradient
        # up to lake water_y). This is what user wants — no perched water.
        from scipy.ndimage import minimum_filter as _min_filter_cap
        from scipy.ndimage import distance_transform_edt as _edt_cap_prop
        # Re-define channel codes locally — CHAN_STREAM is scoped inside
        # the `if lake_mask.any():` block above and may be unbound on
        # lake-less tiles.
        _CHAN_RIVER_CAP = np.uint8(2)
        _CHAN_STREAM_CAP = np.uint8(1)
        river_cells_for_cap = (
            (river_meta == _CHAN_RIVER_CAP) | (river_meta == _CHAN_STREAM_CAP)
        )
        if river_cells_for_cap.any():
            _HIGH_CAP = np.int16(10000)
            _masked_bank = np.where(
                river_cells_for_cap,
                _HIGH_CAP,
                pre_carve_y,
            ).astype(np.int16)
            _min_bank_3x3 = _min_filter_cap(_masked_bank, size=3)
            _edge_with_bank = (
                river_cells_for_cap
                & (_min_bank_3x3 < _HIGH_CAP // 2)
            )
            if _edge_with_bank.any():
                # Propagate edge cap into wide-river interior via EDT
                _, _edge_idx = _edt_cap_prop(
                    ~_edge_with_bank, return_indices=True
                )
                _propagated_cap = (_min_bank_3x3 - np.int16(1))[
                    _edge_idx[0], _edge_idx[1]
                ]
                # EXCEPTION: preserve BLEND-affected cells (within 24
                # blocks of a lake — the v8.12 BLEND_DIST). The BLEND
                # intentionally raises river_water_y toward lake water_y
                # for the visual cascade — capping those would collapse
                # the cascade into a hard step at the lake-river
                # junction. (Hardcoded 24 here matches the BLEND_DIST
                # constant inside the `if lake_mask.any()` block above
                # — keep them in sync.)
                _BLEND_PROTECT_DIST = 24
                if lake_mask.any():
                    _dist_from_lake_cap = _edt_cap_prop(~lake_mask)
                    _is_blend_cell = (_dist_from_lake_cap <= _BLEND_PROTECT_DIST)
                else:
                    _is_blend_cell = np.zeros_like(
                        river_cells_for_cap, dtype=bool
                    )
                _too_high = (
                    river_cells_for_cap
                    & (river_water_y > _propagated_cap)
                    & (_propagated_cap > core_col_gen.SEA_LEVEL)
                    & ~_is_blend_cell
                )
                if _too_high.any():
                    river_water_y[_too_high] = _propagated_cap[_too_high]

        core_chunk.write_tile(
            surface_y    = surface_y,
            surface_blk  = surface_blk,
            sub_blk      = sub_blk,
            ground_cover = ground_cover,
            biome_grid   = biome_grid,
            placements   = placements,
            schem_loader = core_schem_loader,
            tile_world_x = col_off,
            tile_world_z = row_off,
            output_dir   = out_dir,
            cfg          = cfg,
            river_water_y= river_water_y,
            lithology_tile=lithology_tile,
            flow_tile    = masks["flow"],
        )

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "tile_x":     tile_x,
        "tile_y":     tile_y,
        "biomes":     unique_biomes,
        "elapsed_ms": elapsed_ms,
    }


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Vandir World Generation Pipeline — CLI entry point"
    )
    parser.add_argument("--config",      required=True, help="Path to thresholds.json")
    parser.add_argument("--masks",       required=True, help="Directory of 50k mask TIFFs")
    parser.add_argument("--schem-index", required=True, help="Path to schematic_index.json")
    parser.add_argument("--output",      required=True, help="Output directory for .mca files")
    parser.add_argument("--threads",     type=int, default=min(os.cpu_count() or 4, 4),
                        help="Worker process count (default: min(CPU count, 4))")
    parser.add_argument("--tile-x0",    type=int, default=0,   help="Tile X start (inclusive)")
    parser.add_argument("--tile-x1",    type=int, default=None, help="Tile X end (exclusive)")
    parser.add_argument("--tile-z0",    type=int, default=0,   help="Tile Z start (inclusive)")
    parser.add_argument("--tile-z1",    type=int, default=None, help="Tile Z end (exclusive)")
    parser.add_argument("--tile-list",  type=str, default=None,
                        help="S62: comma-separated tx,tz pairs separated by semicolons, "
                             "e.g. '20,53;32,7;59,90'. Overrides --tile-x0..--tile-z1.")
    parser.add_argument("--dry-run",    action="store_true",
                        help="Run all steps but skip chunk writing")
    args = parser.parse_args()

    config_path     = Path(args.config).resolve()
    masks_dir       = Path(args.masks).resolve()
    schem_index_path= Path(getattr(args, "schem_index")).resolve()
    output_dir      = Path(args.output).resolve()

    # Validate inputs
    if not config_path.is_file():
        _log(f"ERROR: config not found: {config_path}")
        return 1
    if not masks_dir.is_dir():
        _log(f"ERROR: masks directory not found: {masks_dir}")
        return 1
    if not schem_index_path.is_file():
        _log(f"ERROR: schematic index not found: {schem_index_path}")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    # Auto-install vandir_height.zip datapack into output/datapacks/
    # so when MCAs are deployed to a world, the height datapack travels
    # with them.  Required because chunk_writer emits 32 sections (Y=-64
    # to Y=448) but vanilla MC 1.21.10 supports only 24 sections without
    # this datapack — chunks crash with ArrayIndexOutOfBoundsException
    # on load.
    project_root = Path(__file__).resolve().parent
    _datapack_src = project_root / "assets" / "vandir_height.zip"
    if _datapack_src.is_file():
        _datapack_dest_dir = output_dir / "datapacks"
        _datapack_dest_dir.mkdir(parents=True, exist_ok=True)
        _datapack_dest = _datapack_dest_dir / "vandir_height.zip"
        try:
            import shutil as _sh
            _sh.copy2(_datapack_src, _datapack_dest)
            _log(f"  datapack:    auto-installed {_datapack_dest.name} → {_datapack_dest_dir}")
        except Exception as _e:
            _log(f"  datapack:    WARN failed to copy ({_e})")
    else:
        _log(f"  datapack:    WARN assets/vandir_height.zip not found — chunks may OOB on load")

    # Ensure /core is importable by workers (project_root defined above)
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # Tile range (or explicit list via --tile-list)
    if args.tile_list:
        tile_coords = []
        for pair in args.tile_list.split(";"):
            pair = pair.strip()
            if not pair:
                continue
            tx_s, tz_s = pair.split(",")
            tile_coords.append((int(tx_s), int(tz_s)))
    else:
        tx0 = args.tile_x0
        tx1 = args.tile_x1 if args.tile_x1 is not None else TILES_PER_AXIS
        tz0 = args.tile_z0
        tz1 = args.tile_z1 if args.tile_z1 is not None else TILES_PER_AXIS
        tile_coords = [
            (tx, tz)
            for tz in range(tz0, tz1)
            for tx in range(tx0, tx1)
        ]
    total = len(tile_coords)

    _log(f"Pipeline start: {total} tiles, {args.threads} workers, dry_run={args.dry_run}")
    _log(f"  config:      {config_path}")
    _log(f"  masks:       {masks_dir}")
    _log(f"  schem_index: {schem_index_path}")
    _log(f"  output:      {output_dir}")

    pipeline_start = time.perf_counter()
    completed = 0
    errors    = 0

    # Build per-tile arg dicts (picklable — no live objects)
    def _make_args(tx: int, tz: int) -> dict:
        return {
            "tile_x":           tx,
            "tile_y":           tz,
            "config_path":      str(config_path),
            "masks_dir":        str(masks_dir),
            "schem_index_path": str(schem_index_path),
            "output_dir":       str(output_dir),
            "tile_size":        TILE_SIZE_PX,
            "dry_run":          args.dry_run,
        }

    with ProcessPoolExecutor(max_workers=args.threads) as pool:
        futures = {
            pool.submit(_process_tile, _make_args(tx, tz)): (tx, tz)
            for tx, tz in tile_coords
        }

        for future in as_completed(futures):
            tx, tz = futures[future]
            _emit({"type": "tile_start", "tile_x": tx, "tile_y": tz})

            try:
                result = future.result()
                completed += 1
                _emit({
                    "type":       "tile_complete",
                    "tile_x":     result["tile_x"],
                    "tile_y":     result["tile_y"],
                    "biomes":     result["biomes"],
                    "elapsed_ms": result["elapsed_ms"],
                })
            except Exception as exc:
                errors += 1
                err_str = f"{type(exc).__name__}: {exc}"
                _log(f"tile ({tx},{tz}) ERROR: {err_str}")
                _log(traceback.format_exc())
                _emit({
                    "type":   "tile_error",
                    "tile_x": tx,
                    "tile_y": tz,
                    "error":  err_str,
                })

    elapsed_s = time.perf_counter() - pipeline_start
    _emit({
        "type":         "pipeline_complete",
        "total_tiles":  total,
        "completed":    completed,
        "errors":       errors,
        "elapsed_s":    round(elapsed_s, 1),
    })

    _log(f"Done: {completed}/{total} tiles OK, {errors} errors, {elapsed_s:.1f}s")
    return 0 if errors == 0 else 1


# ---------------------------------------------------------------------------
# SMOKE TEST  (--dry-run style, no live pipeline modules needed)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # When called directly with no args, run a self-contained argument parse test
    if len(sys.argv) == 1:
        print("run_pipeline.py — argument / IPC smoke test", file=sys.stderr)

        # Test _emit produces valid JSON and flushes
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _emit({"type": "tile_start", "tile_x": 0, "tile_y": 0})
            _emit({"type": "tile_complete", "tile_x": 0, "tile_y": 0,
                   "biomes": ["MIXED_FOREST"], "elapsed_ms": 1234})
            _emit({"type": "pipeline_complete", "total_tiles": 1,
                   "completed": 1, "errors": 0, "elapsed_s": 1.2})

        lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        assert len(lines) == 3, f"Expected 3 IPC lines, got {len(lines)}"
        for line in lines:
            parsed = json.loads(line)
            assert "type" in parsed, f"Missing 'type' key in: {line}"

        # Test tile range logic
        tx0, tx1, tz0, tz1 = 0, 3, 0, 3
        coords = [(tx, tz) for tz in range(tz0, tz1) for tx in range(tx0, tx1)]
        assert len(coords) == 9, f"Expected 9 tiles, got {len(coords)}"
        assert coords[0] == (0, 0)
        assert coords[-1] == (2, 2)

        # Test TILES_PER_AXIS
        assert TILES_PER_AXIS == 97, f"Expected 97 tiles/axis, got {TILES_PER_AXIS}"
        assert TOTAL_TILES == 9409, f"Expected 9409 total tiles, got {TOTAL_TILES}"

        print("  IPC emit:         OK (3 lines, all valid JSON)", file=sys.stderr)
        print("  tile range logic: OK (3×3 = 9 coords)", file=sys.stderr)
        print(f"  world geometry:   OK ({TILES_PER_AXIS}×{TILES_PER_AXIS} = {TOTAL_TILES} tiles)", file=sys.stderr)
        print("PASS", file=sys.stderr)
        sys.exit(0)
    else:
        sys.exit(main())
