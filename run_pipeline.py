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
    core_flow_erosion   = importlib.import_module("core.flow_erosion")
    core_river_settle   = importlib.import_module("core.river_flood_settle")
    core_bank_taper     = importlib.import_module("core.bank_taper")
    core_water_cleanup  = importlib.import_module("core.water_cleanup")
    core_water_fill     = importlib.import_module("core.water_fill")

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
    # S91 regression #1: padded height halo for the ocean-depth EDT so the
    # depth blend is seam-free at underwater coastlines. Pad must cover
    # ocean_depth.transition_px (beyond that the blend saturates and the
    # exact distance no longer matters).
    _od_pad = max(64, int(cfg.get("ocean_depth", {}).get("transition_px", 30)) + 16)
    try:
        _h_pad_masks = core_tile_stream.read_tile(
            masks_dir   = masks_dir,
            col_off     = col_off,
            row_off     = row_off,
            width       = w,
            height      = h,
            pad_px      = _od_pad,
            mask_subset = ("height",),
        )
        height_padded_uint16 = np.round(
            _h_pad_masks["height"] * 65535.0).astype(np.uint16)
        del _h_pad_masks
    except Exception as _od_pad_exc:  # noqa: BLE001
        print(f"[ocean_pad] WARN tile=({tile_x},{tile_y}): "
              f"{type(_od_pad_exc).__name__}: {_od_pad_exc}")
        height_padded_uint16 = None
    surface_y = core_col_gen.generate_columns(
        height_tile  = height_uint16,
        slope_tile   = masks["slope"],
        biome_grid   = biome_grid,
        shore_tile   = masks["shore"],
        noise_fields = noise,
        cfg          = cfg,
        tile_x       = tile_x,
        tile_y       = tile_y,
        height_tile_padded = height_padded_uint16,
        pad_px       = (_od_pad if height_padded_uint16 is not None else 0),
    )
    del height_padded_uint16

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
        hydro_river_bed = masks.get("hydro_river_bed"),  # S83 v8 global bed
        hydro_river_water_y = masks.get("hydro_river_water_y"),  # S83 v9 global water_y
        hydro_dist_src = masks.get("hydro_dist_src"),  # S93e headwater taper
        hydro_dcl      = masks.get("hydro_dcl"),
        hydro_dclpt    = masks.get("hydro_dclpt"),
        hydro_hw_cl    = masks.get("hydro_hw_cl"),
        masks_dir      = masks_dir,
        tile_x         = tile_x,
        tile_z         = tile_y,
    )

    # S89-walk4: bowl-carve lake-bed RE-LOCK snapshot. The carve sets the smooth
    # bowl bed for lake cells (river_meta==CHAN_LAKE==3), but post-carve passes
    # (flow erosion, peak-crunch, bed/bank smoothing) refill the shallow walls
    # (antipattern #2). Snapshot the carved lake bed NOW; re-apply after ALL
    # surface_y smoothing, just before chunk write, so the bowl survives.
    _lake_bowl_relock = bool((cfg or {}).get("hydrology_engine", {})
                             .get("river_geometry", {}).get("lake_bowl_carve", False))
    _lake_bed_lock_y = None
    _lake_bed_lock_mask = None
    if _lake_bowl_relock:
        _lake_bed_lock_mask = (river_meta == 3)
        if _lake_bed_lock_mask.any():
            _lake_bed_lock_y = surface_y.copy()

    # S89 walk: user now WANTS real watered rivers in SAND_DUNE_DESERT (with the
    # usual dirt/mud river palette). The S59 strip is now OPT-IN via config
    # (cfg.sand_dune_desert.strip_rivers, default FALSE = rivers KEEP/carve/fill).
    # Lakes (river_meta == CHAN_LAKE == 3) were always preserved regardless.
    _strip_sdd = bool((cfg or {}).get("sand_dune_desert", {}).get("strip_rivers", False))
    if _strip_sdd:
        _sdd_river = (biome_grid == "SAND_DUNE_DESERT") & (river_meta != 3)
        if _sdd_river.any():
            surface_y[_sdd_river]  = pre_carve_y[_sdd_river]
            river_meta[_sdd_river] = 0

    # ---- Step 6b.0 (S89 walk3): FLOW EROSION — dissect blobby rock massifs ----
    # Cut a drainage texture into rock terrain BEFORE cliff_deg so slope-driven
    # decoration (ground cover, schematic reject) and the written columns all
    # follow the eroded shape. Gated to rock_layers tier>=1, excludes carved
    # rivers. No-op unless cfg.flow_erosion.enabled.
    # S89-walk4 seam fix: feed flow_erosion a padded halo (pre-carve spline
    # surface + flow + rock_layers) so its rock edge-fade EDT + gully smoothing
    # see neighbour-tile rock instead of treating the tile border as a rock
    # boundary -- the per-tile EDT was fading the gully/ridge texture to 0 in a
    # strip at every seam (= the relief-texture seam). Same read_tile loader as
    # the per-tile masks so formats match; pre-carve spline is exact enough for
    # rock cells (carve only touches river/lake). Edge-of-world tiles zero-fill
    # the halo (correct: nothing beyond) and still get the seam-free interior.
    _fe_pad = int((cfg or {}).get("flow_erosion", {}).get("seam_pad_px", 64))
    _fe_syp = _fe_fp = _fe_rp = None
    if (_fe_pad > 0 and masks_dir is not None
            and (cfg or {}).get("flow_erosion", {}).get("enabled", False)):
        try:
            _pm_fe = core_tile_stream.read_tile(
                masks_dir, tile_x * w, tile_y * h, w, h,
                pad_px=_fe_pad, mask_subset=["height", "flow", "rock_layers"])
            _hpn = _pm_fe.get("height")
            if _hpn is not None and _hpn.shape == (h + 2 * _fe_pad, w + 2 * _fe_pad):
                _hri = np.clip((_hpn * 65535.0).astype(np.int32), 0, 65535)
                _fe_syp = np.clip(core_col_gen._LUT[_hri],
                                  core_col_gen.MC_Y_MIN + 4,
                                  core_col_gen.MC_Y_MAX - 1).astype(np.int16)
                _fe_fp = _pm_fe.get("flow")
                _fe_rp = _pm_fe.get("rock_layers")
        except Exception as _fe_exc:  # noqa: BLE001
            print(f"[flow_erosion seam] WARN tile=({tile_x},{tile_y}): "
                  f"{type(_fe_exc).__name__}: {_fe_exc}", flush=True)
            _fe_syp = _fe_fp = _fe_rp = None
    surface_y = core_flow_erosion.apply_flow_erosion(
        surface_y, masks.get("flow"), masks.get("rock_layers"),
        river_meta, cfg, tile_x, tile_y,
        pad=(_fe_pad if _fe_syp is not None else 0),
        surface_y_pad=_fe_syp, flow_pad=_fe_fp, rock_pad=_fe_rp,
    )

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
        snow_gap_physics = masks.get("snow_gap_physics"),
        sand_dunes = masks.get("sand_dunes"),
        beach = masks.get("beach"),
        override_tile = masks.get("override"),
        aspect_tile = masks.get("aspect"),  # S88: rock_gap probability modulator
    )

    # ---- Step 6c: REMOVED S58 ----
    # Both the downslope alpine inheritance (v8, backup branch
    # backup/s58-v8-inheritance) and the ridge watershed override (v9,
    # produced "weird conflicts" in-game) are disabled. Alpine pixels
    # keep their assign_biomes default (SNOWY_BOREAL_TAIGA per
    # OVERRIDE_BIOME_MAP since S56). The soften+dither below handles
    # the visible biome-to-biome transition.

    # ---- Step 6c.5: Soften biome boundaries (S58; REWORKED S93c) ----
    # The inner soften now happens inside Step 6c2 on the HALO'D grid (the
    # razor-seam fix): softening the bare 512² gave the spray EDT a grid-
    # local view — a neighbour-tile biome absent from the window had
    # dist=inf, could never win a spray cell, and its salt-and-pepper
    # stopped DEAD at the tile edge ((27,34)|(28,34): 0 vs 23-40
    # BOREAL_ALPINE cells per column across the seam = a razor-straight
    # biome front in-world). Legacy per-tile soften kept only as the
    # 6c2-failure fallback below.
    _TUNDRA_FLOOR_Y = 625
    _softened_via_halo = False

    def _tundra_floor_remap(_bg):
        # Step 6c.6 (S89 walk): ARCTIC_TUNDRA below the floor reads better
        # as SNOWY_BOREAL_TAIGA. Runs AFTER soften, before all consumers
        # (decorate, schematics, MC tag).
        _at_low = (_bg == "ARCTIC_TUNDRA") & (surface_y < _TUNDRA_FLOOR_Y)
        if _at_low.any():
            _bg[_at_low] = "SNOWY_BOREAL_TAIGA"

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
    # S89-walk4 seam meta-fix: pre-carve neighbour-surface halo for seam-free
    # per-tile surface_y gaussians in decorate_surface. Flag-gated; falls back
    # to per-tile mode='nearest' when off.
    _seam_cfg = (cfg or {}).get("seam_smoothing", {})
    import os as _os_seam
    _seam_on = bool(_seam_cfg.get("enabled", True)) and not _os_seam.environ.get("SEAM_OFF")
    _SEAM_PAD_PX = int(_seam_cfg.get("pad_px", 96))
    surface_y_padded = None
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
        # S93c: the inner biome grid IS the center slice of the halo'd
        # soften — both sides of any seam compute the spray from the same
        # world-coord noise AND the same EDT view (halo 512 >> spray reach
        # ~2*amp=80), so spray density is continuous across tiles.
        biome_grid = _bg_big[
            _INHERITANCE_PAD_PX:_INHERITANCE_PAD_PX + h,
            _INHERITANCE_PAD_PX:_INHERITANCE_PAD_PX + w].copy()
        _tundra_floor_remap(biome_grid)
        _softened_via_halo = True
        # Re-sync the center so ring + inner stay consistent (the ring does
        # not get the tundra remap — pre-existing limitation).
        _bg_big[_INHERITANCE_PAD_PX:_INHERITANCE_PAD_PX + h,
                _INHERITANCE_PAD_PX:_INHERITANCE_PAD_PX + w] = biome_grid
        # Extract inner-plus-48 window for the ecotone dither.
        _lo = _INHERITANCE_PAD_PX - _ECOTONE_PAD_PX
        _hi_r = _INHERITANCE_PAD_PX + h + _ECOTONE_PAD_PX
        _hi_c = _INHERITANCE_PAD_PX + w + _ECOTONE_PAD_PX
        biome_grid_padded = _bg_big[_lo:_hi_r, _lo:_hi_c].copy()
        del _bg_big
        # Build the pre-carve surface halo from the padded height mask (same
        # spline LUT the columns use). Inner is overwritten with the real
        # surface_y right before decorate; only the halo ring feeds the
        # cross-tile gaussians. SEAM_PAD covers the widest smoother sigma (~30).
        if _seam_on:
            _hraw = np.clip(
                (_padded_masks["height"] * 65535.0).astype(np.int32), 0, 65535)
            _sy_pre = core_col_gen._LUT[_hraw].astype(np.float32)
            # S91 regression #1 (residual): apply the ocean-depth correction
            # to the halo too. The raw-LUT halo is up to min_ocean_depth
            # SHALLOWER than the corrected inner at ocean cells, so the
            # decorate seam gaussians smoothed corrected inner ocean floor
            # against an uncorrected ring -> residual 3-4 block underwater
            # steps at tile borders (measured at (38,12)|(39,12)). Mirrors
            # generate_columns' formula on the padded array.
            _od_cfg_h = cfg.get("ocean_depth", {})
            _tr_h = float(_od_cfg_h.get("transition_px", 30))
            _md_h = float(_od_cfg_h.get("min_depth", 15))
            _land_h = _sy_pre >= core_col_gen.SEA_LEVEL
            if (~_land_h).any():
                from scipy.ndimage import distance_transform_edt as _edt_h
                _blend_h = np.clip(
                    _edt_h(~_land_h).astype(np.float32) / _tr_h, 0.0, 1.0)
                _min_h = np.float32(core_col_gen.SEA_LEVEL - _md_h)
                _corr_h = np.minimum(_sy_pre, _min_h)
                _sy_pre = np.where(
                    ~_land_h,
                    np.round(_sy_pre * (1.0 - _blend_h) + _corr_h * _blend_h),
                    _sy_pre).astype(np.float32)
                del _blend_h, _corr_h
            del _land_h
            _slo = _INHERITANCE_PAD_PX - _SEAM_PAD_PX
            surface_y_padded = _sy_pre[
                _slo:_INHERITANCE_PAD_PX + h + _SEAM_PAD_PX,
                _slo:_INHERITANCE_PAD_PX + w + _SEAM_PAD_PX].copy()
            del _sy_pre, _hraw
    except Exception as _ecotone_pad_exc:  # noqa: BLE001
        # Non-fatal: fall back to unpadded ecotone dither + per-tile smoothing.
        print(f"[ecotone_pad] WARN tile=({tile_x},{tile_y}): "
              f"{type(_ecotone_pad_exc).__name__}: {_ecotone_pad_exc}")
        biome_grid_padded = None
        surface_y_padded = None
    if not _softened_via_halo:
        # 6c2 failed before the halo'd soften landed — legacy per-tile
        # soften (razor seams, but better than no softening at all).
        biome_grid = core_biome_assign.soften_biome_boundaries(
            biome_grid, tile_x * w, tile_y * h,
            amplitude_px=40.0, scale=200.0, octaves=2,
        )
        _tundra_floor_remap(biome_grid)

    # ---- Step 6d: Meadow clearing field (S57 Phase 3a) ----
    # Shared low-freq noise field read by both surface_decorator (ground cover
    # + surface block override in clearings) and schematic_placement (tree
    # density suppression).  Single field -> trees and grass clearings align
    # on the same seam deterministically.
    clearing_field = core_clearing.compute_meadow_clearing_field(
        tile_x, tile_y, H=surface_y.shape[0], W=surface_y.shape[1]
    )

    # ---- Step 6e: Rock-gap surface crunch (S87 Phase 2A, S87-tune-1) ----
    # Displace surface_y by per-pixel noise scaled by SLOPE so fade-band rock
    # also crunches (not just gap==5 pixels).  S87 walk found:
    #   - "Rock gap noise isn't generating on all rock gaps, especially faded
    #      areas" -- gap_mask==5 fade band is coin-rolled so noise was patchy.
    #     Solution: drive noise off cliff_deg directly, scale amplitude
    #     by slope across 35-45 deg fade band.
    #   - "a bit too intense" -- reduce default max amplitude from 2 to 1.
    # Deterministic world-coord hash so adjacent tiles share the same value
    # at shared pixels (no tile seams).
    _crunch_cfg = cfg.get("peak_crunch", {}) if isinstance(cfg, dict) else {}
    # S87 walk #3 Phase 2A proper: RE-ENABLED with post-Step9 lock.
    # The earlier disable (S87-fix-19/20) was because Step 9 water/lake
    # fixes + gaussian smoothing partially un-did the displacement after
    # schematic placement saw the displaced Y.  Fix: save the displaced
    # surface_y snapshot here and re-apply it at rock pixels AFTER Step 9
    # finishes (just before chunk_writer.write_tile).  Schematics anchor
    # at the displaced Y; chunk_writer emits columns at the SAME displaced
    # Y.  Trees no longer float; trunk_extension doesn't hit max 6 blocks.
    _crunch_enabled = bool(_crunch_cfg.get("enabled", True))
    _crunch_lock_y: "np.ndarray | None" = None
    _crunch_rock_mask: "np.ndarray | None" = None
    if (_crunch_enabled and eco_grads is not None
            and cliff_deg is not None):
        _crunch_amp = int(_crunch_cfg.get("amplitude_blocks", 1))
        _fade_start = float(_crunch_cfg.get("slope_fade_start_deg", 35.0))
        _slope_full = float(_crunch_cfg.get("slope_full_deg", 45.0))
        # S87 walk #4 v4: turn intensity DOWN + fades UP.
        _river_fade_blocks = float(_crunch_cfg.get("river_fade_blocks", 14.0))  # was 8
        # S87 walk #4 v6: prob_cap halved 0.5 -> 0.25 per user.  Even at full
        # slope, only 25% of eligible pixels get +/-amp.  Sparser noise.
        _prob_cap = float(_crunch_cfg.get("probability_cap", 0.25))
        import numpy as _np_crunch
        _H, _W = surface_y.shape
        # Amplitude scalar per pixel: 0 below fade_start, 1 above slope_full.
        _amp_scale = _np_crunch.clip(
            (cliff_deg - _fade_start) / max(0.1, _slope_full - _fade_start),
            0.0, 1.0,
        ).astype(_np_crunch.float32)
        # S87 walk #4 (36,15): river-aware fade.  river_meta only marks
        # WATER cells, but the carver also lowered terrain around the water
        # to form banks/walls; those bank pixels are NOT in river_meta but
        # are visually part of the river feature.  DILATE river_meta by a
        # bank radius first so the no-noise zone spans water+banks+walls.
        # Then apply distance-fade beyond that.
        # User: "exception river zones from noise with a smoothed faded end
        # into it OUTSIDE of the bounds of the river, bank, and walls."
        if river_meta is not None and (river_meta > 0).any():
            from scipy.ndimage import distance_transform_edt as _dt_river
            from scipy.ndimage import binary_dilation as _bd_river
            _bank_radius = int(_crunch_cfg.get("river_bank_blocks", 8))  # was 5
            _river_zone = _bd_river(river_meta > 0, iterations=_bank_radius)
            _river_dist = _dt_river(~_river_zone).astype(_np_crunch.float32)
            _river_fade = _np_crunch.clip(
                _river_dist / max(0.5, _river_fade_blocks), 0.0, 1.0
            )
            _amp_scale = _amp_scale * _river_fade
            del _river_dist, _river_fade, _river_zone
        # S88: HARD wash exclusion (was 6-block fade).  Washes are flow-driven
        # sediment surfaces -- they must be SMOOTH, never noisy.  Match the
        # wash painter's mask (rock_px & flow>min_flow) AND its dilation so
        # every pixel that surface_decorator paints as a wash also gets zero
        # crunch noise, plus a 2-block buffer ring outside the painter zone.
        # User: "washes should NOT get the noise layer.  they should be smooth."
        if eco_grads is not None and hasattr(eco_grads, "gap_mask"):
            _gap = eco_grads.gap_mask
            _flow = masks.get("flow") if isinstance(masks, dict) else None
            if _flow is not None:
                _wcfg_p2a = cfg.get("washes", {}) if isinstance(cfg, dict) else {}
                _wash_min_flow = float(_wcfg_p2a.get("min_flow", 0.002))
                _wash_paint_dilation = int(_wcfg_p2a.get("dilation", 2))
                _wash_core = (_gap == 5) & (_flow > _wash_min_flow)
                if _wash_core.any():
                    from scipy.ndimage import binary_dilation as _bd_wash
                    # Match wash painter dilation + 2-block buffer outside,
                    # so even the painter's edge-fade pixels stay noise-free.
                    _wash_zone_full = _bd_wash(
                        _wash_core,
                        iterations=_wash_paint_dilation + 2,
                    )
                    _amp_scale[_wash_zone_full] = 0.0
                    del _wash_core, _wash_zone_full
        # S88: talus exclusion — talus aprons are soft/depositional, like
        # washes.  Cliff_cap and bedrock_drainage are NOT excluded (hard
        # erosion, block-scale weathering is realistic).
        _talus_tile = masks.get("talus_apron") if isinstance(masks, dict) else None
        if _talus_tile is not None:
            _talus_cfg_p2a = cfg.get("lithology", {}).get("talus", {})
            if bool(_talus_cfg_p2a.get("phase2a_exclude", True)):
                _talus_thr = int(_talus_cfg_p2a.get("intensity_threshold", 64))
                _talus_byte = (_talus_tile * 255.0).astype(_np_crunch.int32)
                _amp_scale[_talus_byte >= _talus_thr] = 0.0
                del _talus_byte
        if (_amp_scale > 0).any():
            # splitmix64 hash on world (x, z) -> uniform [0, 1)
            _wx = (tile_x * _W + _np_crunch.arange(_W, dtype=_np_crunch.uint64))[None, :]
            _wz = (tile_y * _H + _np_crunch.arange(_H, dtype=_np_crunch.uint64))[:, None]
            _hh = (_wx * _np_crunch.uint64(0x9E3779B97F4A7C15)
                   + _wz * _np_crunch.uint64(0xBF58476D1CE4E5B9))
            _hh = (_hh ^ (_hh >> _np_crunch.uint64(30))) * _np_crunch.uint64(0xBF58476D1CE4E5B9)
            _hh = (_hh ^ (_hh >> _np_crunch.uint64(27))) * _np_crunch.uint64(0x94D049BB133111EB)
            _hh = _hh ^ (_hh >> _np_crunch.uint64(31))
            _u01 = (_hh.astype(_np_crunch.float64)
                    / _np_crunch.float64(_np_crunch.iinfo(_np_crunch.uint64).max)).astype(_np_crunch.float32)
            # S87 walk #4: PROBABILISTIC displacement -- amp_scale is the
            # PROBABILITY a pixel gets full +/-crunch_amp, not a multiplier
            # on amplitude.  Previously _disp = amp_scale * amp rounded to
            # 0 at low amp_scale (~slope < 40deg), producing a sharp
            # "noise -> smooth" cliff at ~40deg even though amp_scale was
            # smoothly fading.  User: "noise boundary to smooth land
            # boundary is also still there".  Probabilistic approach gives
            # SPARSE noise at low slope (e.g. 30deg: 25% of pixels +/-amp,
            # 75% unchanged) tapering to DENSE noise at high slope.
            # Second hash for the bit that decides apply-or-not.
            _hh2 = (_wx * _np_crunch.uint64(0xD1B54A32D192ED03)
                    + _wz * _np_crunch.uint64(0xA24BAED4963EE407))
            _hh2 = (_hh2 ^ (_hh2 >> _np_crunch.uint64(31))) * _np_crunch.uint64(0x9E3779B97F4A7C15)
            _hh2 = _hh2 ^ (_hh2 >> _np_crunch.uint64(32))
            _u_apply = (_hh2.astype(_np_crunch.float64)
                        / _np_crunch.float64(_np_crunch.iinfo(_np_crunch.uint64).max)).astype(_np_crunch.float32)
            # S87 walk #4 v4: cap probability at _prob_cap so even at full
            # slope we get sparser noise (less intense).
            _apply = _u_apply < (_amp_scale * _prob_cap)
            _signed = _np_crunch.where(_u01 < 0.5, -_crunch_amp, _crunch_amp).astype(_np_crunch.int16)
            _disp_int = _np_crunch.where(_apply, _signed, 0).astype(_np_crunch.int16)
            # S88 smoothing rework.  User: "smoothing appears to LESSEN at
            # borders -- it should INCREASE at the borders of the rock_gap
            # mask" + "increase smoothing across the board".  Three-part fix:
            #   (1) Base gaussian sigma 1.5 -> 2.5 -- broader averaging
            #       smooths ALL noise, not just isolated +/-1 spikes.
            #   (2) Smooth weight base 0.5 -> 0.65 so the core noise is also
            #       visibly smoothed.  Edge weight goes all the way to 1.0.
            #   (3) NEW second pass: gaussian smoothing on the FINAL surface_y
            #       (not just the displacement) across the boundary zone
            #       (bell curve peaks at amp_scale=0.5).  This is the
            #       "smoothing INCREASES at rock_gap borders" the user asked
            #       for -- spatial feathering of the transition between
            #       noisy rock and smooth lowland, not just weighted blending
            #       of displacement values.
            from scipy.ndimage import gaussian_filter as _gf_crunch
            _disp_f = _disp_int.astype(_np_crunch.float32)
            # S89-walk4 seam fix: smooth the +/-crunch displacement on a padded
            # halo so the gaussian sees neighbour-tile crunch instead of
            # replicating the tile edge -> the +/-1 relief TEXTURE is continuous
            # across the seam. Halo crunch = slope-only (river/wash/talus
            # exclusions are inner-only); the inner displacement is preserved.
            _ds_done = False
            if surface_y_padded is not None:
                try:
                    _pp = _SEAM_PAD_PX
                    _Hp, _Wp = surface_y_padded.shape
                    _cdp = core_eco.compute_cliff_deg(
                        surface_y_padded.astype(surface_y.dtype))
                    _aspd = _np_crunch.clip(
                        (_cdp - _fade_start) / max(0.1, _slope_full - _fade_start),
                        0.0, 1.0).astype(_np_crunch.float32)
                    _wxp = (tile_x * _W - _pp
                            + _np_crunch.arange(_Wp, dtype=_np_crunch.uint64))[None, :]
                    _wzp = (tile_y * _H - _pp
                            + _np_crunch.arange(_Hp, dtype=_np_crunch.uint64))[:, None]
                    _h1 = (_wxp * _np_crunch.uint64(0x9E3779B97F4A7C15)
                           + _wzp * _np_crunch.uint64(0xBF58476D1CE4E5B9))
                    _h1 = (_h1 ^ (_h1 >> _np_crunch.uint64(30))) * _np_crunch.uint64(0xBF58476D1CE4E5B9)
                    _h1 = (_h1 ^ (_h1 >> _np_crunch.uint64(27))) * _np_crunch.uint64(0x94D049BB133111EB)
                    _h1 = _h1 ^ (_h1 >> _np_crunch.uint64(31))
                    _u01p = (_h1.astype(_np_crunch.float64)
                             / _np_crunch.float64(_np_crunch.iinfo(_np_crunch.uint64).max)).astype(_np_crunch.float32)
                    _h2 = (_wxp * _np_crunch.uint64(0xD1B54A32D192ED03)
                           + _wzp * _np_crunch.uint64(0xA24BAED4963EE407))
                    _h2 = (_h2 ^ (_h2 >> _np_crunch.uint64(31))) * _np_crunch.uint64(0x9E3779B97F4A7C15)
                    _h2 = _h2 ^ (_h2 >> _np_crunch.uint64(32))
                    _uapp = (_h2.astype(_np_crunch.float64)
                             / _np_crunch.float64(_np_crunch.iinfo(_np_crunch.uint64).max)).astype(_np_crunch.float32)
                    _dispp = _np_crunch.where(
                        _uapp < (_aspd * _prob_cap),
                        _np_crunch.where(_u01p < 0.5, -_crunch_amp, _crunch_amp),
                        0).astype(_np_crunch.float32)
                    _dispp[_pp:_pp + _H, _pp:_pp + _W] = _disp_f   # inner = exclusion-applied
                    _disp_smooth = _gf_crunch(_dispp, sigma=2.5)[_pp:_pp + _H, _pp:_pp + _W]
                    _ds_done = True
                    del _cdp, _aspd, _wxp, _wzp, _h1, _h2, _u01p, _uapp, _dispp
                except Exception:
                    _ds_done = False
            if not _ds_done:
                _disp_smooth = _gf_crunch(_disp_f, sigma=2.5)
            # Smooth weight: 0.65 at core (always-on), +0.35 extra at fade
            # edges (so amp_scale=0 -> _sw=1.0 = fully smoothed).
            _sw = (0.65 + 0.35 * (1.0 - _amp_scale)).astype(_np_crunch.float32)
            _disp_blended = _disp_f * (1.0 - _sw) + _disp_smooth * _sw
            _disp_int = _np_crunch.round(_disp_blended).astype(_np_crunch.int16)
            _new_y = surface_y.astype(_np_crunch.int16) + _disp_int
            _land = surface_y > 63
            _displace_mask = _land & (_disp_int != 0)
            surface_y = _np_crunch.where(
                _displace_mask,
                _new_y, surface_y.astype(_np_crunch.int16)
            ).astype(surface_y.dtype)

            # ---- Boundary surface_y smoothing pass (S88) ----
            # Bell curve weight: 0 at amp_scale=0 (pure smooth lowland) and
            # at amp_scale=1 (pure rock core), peaks at amp_scale=0.5 (the
            # mid-fade transition).  Spatial gaussian on surface_y itself
            # feathers the boundary between noisy and smooth regions.
            _amp_bell = (4.0 * _amp_scale * (1.0 - _amp_scale)).astype(
                _np_crunch.float32)
            _amp_bell = _np_crunch.clip(_amp_bell, 0.0, 1.0)
            if float(_amp_bell.max()) > 0.05:
                # S89-walk4 seam fix: feather on the padded halo so the boundary
                # surface smoothing is continuous across tile borders.
                if surface_y_padded is not None:
                    _pp2 = _SEAM_PAD_PX
                    _wrk = surface_y_padded.astype(_np_crunch.float32).copy()
                    _wrk[_pp2:_pp2 + _H, _pp2:_pp2 + _W] = surface_y.astype(_np_crunch.float32)
                    _sy_smooth_pass = _gf_crunch(_wrk, sigma=3.0)[_pp2:_pp2 + _H, _pp2:_pp2 + _W]
                    del _wrk
                else:
                    _sy_smooth_pass = _gf_crunch(
                        surface_y.astype(_np_crunch.float32), sigma=3.0
                    )
                _sy_blend = (
                    surface_y.astype(_np_crunch.float32) * (1.0 - _amp_bell)
                    + _sy_smooth_pass * _amp_bell
                )
                _new_sy_smoothed = _np_crunch.round(_sy_blend).astype(
                    surface_y.dtype)
                # Only land (not ocean) and only where the bell weight is
                # meaningful (>5%) -- avoids touching pure rock core or
                # pure non-rock lowland.
                _bell_land = (surface_y > 63) & (_amp_bell > 0.05)
                surface_y = _np_crunch.where(
                    _bell_land, _new_sy_smoothed, surface_y
                )
                # Lock these pixels too: the boundary smoothing must survive
                # Step 9 just like the displacement does.
                _boundary_smooth_mask = _bell_land
                del _sy_smooth_pass, _sy_blend, _new_sy_smoothed, _bell_land
            else:
                _boundary_smooth_mask = _np_crunch.zeros_like(
                    _displace_mask, dtype=bool)

            # Snapshot AFTER both displacement AND boundary smoothing: the
            # lock at end of Step 9 restores the full Phase 2A result.
            _crunch_lock_y = surface_y.copy()
            _crunch_rock_mask = (_displace_mask | _boundary_smooth_mask)
            del _wx, _wz, _hh, _hh2, _u01, _u_apply, _apply, _signed
            del _disp_int, _disp_f, _disp_smooth, _sw, _disp_blended
            del _new_y, _land, _displace_mask, _amp_bell, _boundary_smooth_mask
        del _amp_scale

    # ---- Step 7: Surface decoration ----
    _use_geo = bool(cfg.get("lithology", {}).get("feature_flag_enabled", False))
    _use_sp  = bool(cfg.get("surface_pipeline", {}).get("feature_flag_enabled", False))
    # S89-walk4: capture the FULLY pre-carve padded surface (inner+halo identical
    # to neighbours, from the global height spline) BEFORE we overwrite the inner.
    # rock_relief uses it for its smooth_gain roughness so the relief amplitude is
    # seamless across rock tile borders (the asymmetric post-crunch-inner vs
    # pre-carve-halo version caused the relief height STEP on rock seams).
    _relief_rough_pad = surface_y_padded.copy() if surface_y_padded is not None else None
    # Stamp the FINAL (post-carve/crunch) surface_y into the halo's inner so the
    # seam gaussians smooth real inner terrain against the pre-carve neighbour
    # halo ring -> continuous across tile borders.
    if surface_y_padded is not None:
        surface_y_padded[_SEAM_PAD_PX:_SEAM_PAD_PX + h,
                         _SEAM_PAD_PX:_SEAM_PAD_PX + w] = surface_y
    import os as _os_surf_pre
    _sy_before_decorate = (surface_y.copy()
                           if _os_surf_pre.environ.get("SURF_DUMP_DIR") else None)  # SURF_DUMP seam-bisect snapshot
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
        # S88: 3 terrain-derived rock-variant masks (cap, talus, bedrock).
        # decorate_surface will skip painting where the tile is None.
        cliff_cap_tile = masks.get("cliff_cap"),
        talus_apron_tile = masks.get("talus_apron"),
        bedrock_drainage_tile = masks.get("bedrock_drainage"),
        vein_field_tile = masks.get("vein_field"),
        varnish_field_tile = masks.get("varnish_field"),
        joint_pattern_tile = masks.get("joint_pattern"),
        rock_layers_tile = masks.get("rock_layers"),
        snow_potential_tile = masks.get("snow_potential"),
        surface_y_padded = surface_y_padded,
        seam_pad_px = (_SEAM_PAD_PX if surface_y_padded is not None else 0),
        relief_rough_padded = _relief_rough_pad,
    )

    # S89 floating-tree fix: snapshot surface_y AFTER decorate (post rock-relief /
    # grass-terrace).  Schematics (Step 8) anchor on THIS surface; Step 9's
    # water/bank/bed smoothing then perturbs surface_y and -- with peak_crunch
    # OFF the old crunch-lock is inactive -- chunk_writer would build columns at
    # the lowered Y, leaving trunks floating.  We restore this snapshot at LAND
    # cells away from water just before write_tile so columns match the anchors;
    # water/bank cells keep Step 9's fixes.
    _post_decorate_y = surface_y.copy()

    # SURF_DUMP seam-bisect hook: dump pre/post-decorate surface_y + the
    # pre-carve neighbour halo, then return early (no schematics, no chunk
    # write).  Lets us reproduce/localise the tile-boundary height seam
    # locally at the surface_y level (no 768-deep volume -> no OOM).
    import os as _os_surf_dump
    _surf_dump_dir = _os_surf_dump.environ.get("SURF_DUMP_DIR")
    if _surf_dump_dir:
        import numpy as _np_sd
        _os_surf_dump.makedirs(_surf_dump_dir, exist_ok=True)
        _np_sd.save(f"{_surf_dump_dir}/sy_pre_{tile_x}_{tile_y}.npy", _sy_before_decorate)
        _np_sd.save(f"{_surf_dump_dir}/sy_post_{tile_x}_{tile_y}.npy", _post_decorate_y)
        _np_sd.save(f"{_surf_dump_dir}/snow_{tile_x}_{tile_y}.npy",
                    np.isin(surface_blk, ("snow_block", "snow", "powder_snow")))
        if river_meta is not None:
            _np_sd.save(f"{_surf_dump_dir}/rmeta_{tile_x}_{tile_y}.npy",
                        np.asarray(river_meta, dtype=np.uint8))
        _np_sd.save(f"{_surf_dump_dir}/sblk_{tile_x}_{tile_y}.npy",
                    np.asarray(surface_blk, dtype=object), allow_pickle=True)
        # S91 veg-seam assess: ground cover + (env-gated) schematic placements.
        _np_sd.save(f"{_surf_dump_dir}/gc_{tile_x}_{tile_y}.npy",
                    np.asarray(ground_cover, dtype=object), allow_pickle=True)
        # S93c ecotone gate: biome grid (diffs must hug boundaries/edges).
        _np_sd.save(f"{_surf_dump_dir}/bg_{tile_x}_{tile_y}.npy",
                    np.asarray(biome_grid, dtype=object), allow_pickle=True)
        if _os_surf_dump.environ.get("SURF_DUMP_SCHEM"):
            try:
                _sd_index = core_placement.load_index(_Path(args["schem_index_path"]))
            except Exception:
                _sd_index = {}
            _sd_plc = core_placement.place_schematics(
                surface_y    = surface_y,
                biome_grid   = biome_grid,
                river_meta   = river_meta,
                moisture_tile= masks["flow"],
                noise_fields = noise,
                cfg          = cfg,
                index        = _sd_index,
                tile_x       = tile_x,
                tile_y       = tile_y,
                eco_grads    = eco_grads,
                cliff_deg    = cliff_deg,
                clearing_field = clearing_field,
                surface_blocks = surface_blk,
                cliff_cap_tile = masks.get("cliff_cap"),
                biome_grid_padded = biome_grid_padded,  # S93c cross-tile ecotone
            )
            _np_sd.save(f"{_surf_dump_dir}/plc_{tile_x}_{tile_y}.npy",
                        np.asarray([(p.world_x, p.world_z, p.place_y, p.size,
                                     p.schem_type, p.species, p.schem_path)
                                    for p in _sd_plc], dtype=object),
                        allow_pickle=True)
        if surface_y_padded is not None:
            _np_sd.save(f"{_surf_dump_dir}/sy_halo_{tile_x}_{tile_y}.npy", surface_y_padded)
        if _relief_rough_pad is not None:
            _np_sd.save(f"{_surf_dump_dir}/rrp_{tile_x}_{tile_y}.npy", _relief_rough_pad)
        return {"tile_x": tile_x, "tile_y": tile_y, "biomes": [],
                "elapsed_ms": 0, "surf_dump": True}

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
        cliff_cap_tile = masks.get("cliff_cap"),
        biome_grid_padded = biome_grid_padded,  # S93c cross-tile ecotone
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
                # S84: read spline from config (was hardcoded OLD spline
                # [0,17050,45000,65496]→[-64,63,200,448]). With new spline
                # column_generator places terrain at new Y values; this
                # mapped lake water_y to OLD Y, leaving lakes 4+ blocks
                # above surrounding terrain. Now matches column LUT.
                _sp_cfg = cfg.get("terrain_spline", {})
                _gaea_in = np.array(_sp_cfg.get("gaea_in",
                                                [0, 17050, 45000, 65496]),
                                    dtype=np.float64)
                _mc_y_out = np.array(_sp_cfg.get("mc_y_out",
                                                 [-64, 63, 200, 448]),
                                     dtype=np.float64)
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

            # ── S89 #12: FORCE-FLOOD shallow DRY lake basins ──────────────────
            # v26 sets lake water to the MIN spill (anti-spillover). A basin whose
            # FLOOR sits at/above that level renders DRY (~78/117 lakes). For each
            # PAINTED lake basin, lower floor cells that are within force_cap ABOVE
            # the water down to water-1 and tag them lake, so they hold water.
            # BOUNDED: only LOWERS surface, capped (skips the rim -> no spillover /
            # no towers), never raises water (respects v26 min-spill). Working lakes
            # (floor already below water) keep their floor; only shoreline cells
            # within force_cap flood (slight shoreline grow -> VERIFY on render).
            # Config: river_carve.dry_lake_force_carve_max (0 = off).
            # !! BLIND first-pass vs the v23/v25/v26 fail-history. RENDER-VERIFY:
            #    lakes fill, NO spillover onto banks, the 39 working lakes intact.
            _hl_ff = masks.get("hydro_lake")
            _ffcap = int(cfg.get("river_carve", {}).get("dry_lake_force_carve_max", 4))
            if _hl_ff is not None and _wl_mc_float is not None and _ffcap > 0:
                for _lid in np.unique(_hl_ff[_hl_ff > 0]).tolist():
                    _basin = (_hl_ff == _lid)
                    _wv = _wl_mc_float[_basin]; _wv = _wv[_wv > -64.0]
                    if _wv.size == 0:
                        continue
                    _wlvl = int(np.floor(float(_wv.min())))
                    if _wlvl < int(core_col_gen.SEA_LEVEL):
                        continue
                    _dry = (_basin & (surface_y >= np.int16(_wlvl))
                            & (surface_y <= np.int16(_wlvl + _ffcap)))
                    if _dry.any():
                        surface_y[_dry]     = np.int16(_wlvl - 1)
                        river_meta[_dry]    = CHAN_LAKE
                        river_water_y[_dry] = np.int16(_wlvl)

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
        # === S82 ITEM #1: TILE-BOUNDARY PADDING ===
        # Run the escape-fix + EDT berm + v8.14 cap passes on PADDED
        # arrays (inner h×w + _PAD pixels of neighbor data on each side).
        # Without padding, scipy's gaussian / maximum / minimum filters
        # reflect or zero-fill past the tile boundary, and EDT computes
        # distance only within the tile — so a water cell 1 cell across
        # a tile boundary looks "infinitely far" to the inner edge cell,
        # producing a harsh single-X seam at every tile boundary.
        #
        # Pad inputs (inner = computed in-memory, outer = approximation):
        #   pre_carve_y_pad: from height.tif at padded coords → LUT → MC Y
        #   surface_y_pad:   copy of pre_carve_y_pad (no carve in pad
        #                    is acceptable since we discard pad output)
        #   river_water_y_pad: -999 default. Painted-lake (hydro_region.png
        #                    id=1) → precompute lake_wl in MC Y. Painted-
        #                    river (id=2) → pre_carve_y - 1 (rough).
        #   river_meta-like (river_cells_pad / lake_mask_pad): from
        #                    painted ids 1/2 in pad region.
        # Inner h×w of every pad array is then overwritten with the
        # authoritative computed values before the passes run.
        #
        # After all 3 passes, crop pad arrays back to the inner h×w and
        # write surface_y / river_water_y in place. The escape-fix /
        # berm modifications inside the pad region itself are discarded
        # (they only existed to inform inner-edge behaviour).
        _PAD = 48
        _H, _W = surface_y.shape
        _PH = _H + 2 * _PAD
        _PW = _W + 2 * _PAD

        # Read height + hydro_lake_wl at padded coords (rasterio Window).
        _padded_masks_io = core_tile_stream.read_tile(
            masks_dir   = masks_dir,
            col_off     = col_off,
            row_off     = row_off,
            width       = w,
            height      = h,
            pad_px      = _PAD,
            mask_subset = ("height", "hydro_lake_wl"),
        )
        _height_pad_norm = _padded_masks_io["height"]
        _lake_wl_pad_norm = _padded_masks_io.get("hydro_lake_wl")
        if _lake_wl_pad_norm is None:
            _lake_wl_pad_norm = np.zeros((_PH, _PW), dtype=np.float32)

        # S84 FIX: use core_col_gen._LUT (which reads terrain_spline from
        # config/thresholds.json at module load). Previously this block had
        # a hardcoded 4-point LUT [0,17050,45000,65496]→[-64,63,200,448]
        # that diverged from the live 13-point realistic spline by 10-36 Y
        # in the inland range. Inner tile used the new spline (via
        # column_generator) but this pad ring used the old hardcoded one;
        # the step at the inner/pad boundary caused escape-fix to wall
        # inner-edge cells to "contain" perceived water spillage. Fix:
        # share the same 65536-entry _LUT.
        _height_raw_pad_int = np.clip(
            (_height_pad_norm * 65535.0).astype(np.int32), 0, 65535
        )
        _pre_carve_pad = np.clip(
            core_col_gen._LUT[_height_raw_pad_int],
            core_col_gen.MC_Y_MIN + 4, core_col_gen.MC_Y_MAX - 1,
        ).astype(np.int16)
        # Overwrite inner with the authoritative pre_carve_y (which
        # includes ocean-depth correction + dune offset from
        # generate_columns — those don't matter for the river/lake
        # cells the cap touches, but be exact where we can).
        _pre_carve_pad[_PAD:_PAD + _H, _PAD:_PAD + _W] = pre_carve_y

        # Lake water_y in MC Y space for the pad region. Same _LUT.
        # Cells with no lake (lake_wl_norm == 0) produce wl_mc < SEA_LEVEL
        # after interpolation, which we treat as "no water" below.
        _lake_wl_raw_int = np.clip(
            (_lake_wl_pad_norm * 65535.0).astype(np.int32), 0, 65535
        )
        _lake_wl_mc_pad = core_col_gen._LUT[_lake_wl_raw_int].astype(np.float32)

        # Read hydro_region.png at 8k, nearest-sample onto padded 50k coords
        # so we know which pad cells are painted-river / painted-lake.
        _hr_path = masks_dir / "hydro_region.png"
        _paint_river_pad = np.zeros((_PH, _PW), dtype=bool)
        _paint_lake_pad = np.zeros((_PH, _PW), dtype=bool)
        if _hr_path.exists():
            try:
                from PIL import Image as _PILImg
                _hr_arr8k = np.asarray(
                    _PILImg.open(_hr_path).convert("L"), dtype=np.uint8
                )
                if _hr_arr8k.shape == (8192, 8192):
                    _S_50K_TO_8K = 8192.0 / 50000.0
                    _ys_pad = np.arange(_PH) + (row_off - _PAD)
                    _xs_pad = np.arange(_PW) + (col_off - _PAD)
                    _ys8 = np.clip(
                        (_ys_pad * _S_50K_TO_8K).astype(np.int32), 0, 8191
                    )
                    _xs8 = np.clip(
                        (_xs_pad * _S_50K_TO_8K).astype(np.int32), 0, 8191
                    )
                    _yy_pad, _xx_pad = np.meshgrid(_ys8, _xs8, indexing="ij")
                    _hr_sampled_pad = _hr_arr8k[_yy_pad, _xx_pad]
                    _paint_river_pad = (_hr_sampled_pad == 2)
                    _paint_lake_pad = (_hr_sampled_pad == 1) & ~_paint_river_pad
            except Exception as _hr_exc:  # noqa: BLE001
                print(f"[s82_pad] WARN paint sample failed tile=({tile_x},"
                      f"{tile_y}): {type(_hr_exc).__name__}: {_hr_exc}",
                      file=sys.stderr, flush=True)

        # surface_y_pad: start from pre_carve, then overwrite inner.
        _surface_y_pad = _pre_carve_pad.copy()
        _surface_y_pad[_PAD:_PAD + _H, _PAD:_PAD + _W] = surface_y

        # S91 #5 bisect instrumentation: per-pass surface snapshots (inner
        # crop), env-gated — same env as the final Step-9 dump.
        import os as _os_s9b
        _s9_bisect = _os_s9b.environ.get("SURF_DUMP_STEP9_DIR")

        def _s9_snap(tag, arr):
            if _s9_bisect:
                np.save(f"{_s9_bisect}/snap_{tag}_{tile_x}_{tile_y}.npy",
                        arr[_PAD:_PAD + _H, _PAD:_PAD + _W])

        _s9_snap("p0_start", _surface_y_pad)

        # river_water_y_pad: -999 default, painted-lake → lake_wl,
        # painted-river → pre_carve - 1. Then overwrite inner.
        _river_water_y_pad = np.full((_PH, _PW), np.int16(-999), dtype=np.int16)
        _has_lake_wl = _paint_lake_pad & (_lake_wl_mc_pad > core_col_gen.SEA_LEVEL)
        _river_water_y_pad[_has_lake_wl] = _lake_wl_mc_pad[_has_lake_wl].astype(np.int16)
        _river_water_y_pad[_paint_river_pad] = (
            _pre_carve_pad[_paint_river_pad] - np.int16(1)
        )
        _river_water_y_pad[_PAD:_PAD + _H, _PAD:_PAD + _W] = river_water_y

        # S94: clean carver source/platform water (pre-cap) for flood-settle.
        _clean_water_pad_s94 = _river_water_y_pad.copy()

        # lake_mask_pad: painted-lake in pad, authoritative lake_mask in inner.
        _lake_mask_pad = _paint_lake_pad.copy()
        _lake_mask_pad[_PAD:_PAD + _H, _PAD:_PAD + _W] = lake_mask

        # river_cells_pad: painted-river in pad, river_meta in inner.
        _CHAN_RIVER_CAP = np.uint8(2)
        _CHAN_STREAM_CAP = np.uint8(1)
        _river_cells_pad = _paint_river_pad.copy()
        _river_cells_pad[_PAD:_PAD + _H, _PAD:_PAD + _W] = (
            (river_meta == _CHAN_RIVER_CAP) | (river_meta == _CHAN_STREAM_CAP)
        )

        # ── Pass 0.05: v8.14 water-level cap (S91 #5: moved to the VERY TOP
        # of the padded block — every later pass consumes water_y, and two
        # of them RAISE land to it: the escape-fix/berm (containment) and,
        # critically, the v15 bank smooth whose "never below nearest water"
        # clamp raises banks to the water level (v4 bisect: ALL +2/+3 levee
        # cells were built in the v13/v16/v15 span, before containment even
        # ran). Capping after any of them leaves orphaned levees. ──
        # S81 v8.14: in rivers, water_y must NEVER be at or above the
        # adjacent bank elevation — otherwise the surface passes wall the
        # banks artificially.
        # S91 #5 second defect: the old blanket BLEND-zone exemption
        # (<= 24px of a lake, for the S81 cascade visual) is replaced by a
        # FLOOR at the nearest lake's water level: in the blend zone the
        # cap becomes max(natural-bank cap, lake level). The S81
        # lake-above-river cascade is preserved by construction (water
        # descending FROM a high lake is <= lake level everywhere), while
        # approach streams can no longer ride 3-5 blocks above a
        # flat-at-spill plain on a walled causeway ("stream on a levee" =
        # the S90 retaining-wall + trench regression, reproduced at (19,76)).
        # S91 #5 third defect: cap against the CURRENT (post-decorate)
        # surface, not pre_carve_y — flow-erosion + decorate lower banks
        # 2-3 blocks below pre-carve heights on flat plains, so a
        # pre-carve-anchored cap let water ride exactly that much above the
        # REAL banks. At this point _surface_y_pad is the pristine
        # post-decorate surface (no pass has touched it) = the honest bank
        # reference. Pad ring still carries pre-carve heights (no decorate
        # in the ring) — same approximation as before.
        from scipy.ndimage import minimum_filter as _min_filter_cap
        from scipy.ndimage import distance_transform_edt as _edt_cap_prop
        if _river_cells_pad.any():
            _HIGH_CAP = np.int16(10000)
            _masked_bank_pad = np.where(
                _river_cells_pad,
                _HIGH_CAP,
                _surface_y_pad,
            ).astype(np.int16)
            _min_bank_3x3_pad = _min_filter_cap(_masked_bank_pad, size=3)
            _edge_with_bank_pad = (
                _river_cells_pad
                & (_min_bank_3x3_pad < _HIGH_CAP // 2)
            )
            if _edge_with_bank_pad.any():
                _, _edge_idx_pad = _edt_cap_prop(
                    ~_edge_with_bank_pad, return_indices=True
                )
                _propagated_cap_pad = (_min_bank_3x3_pad - np.int16(1))[
                    _edge_idx_pad[0], _edge_idx_pad[1]
                ]
                _BLEND_PROTECT_DIST = 24
                _eff_cap_pad = _propagated_cap_pad
                if _lake_mask_pad.any():
                    _dist_from_lake_cap_pad, _lk_idx_cap_pad = _edt_cap_prop(
                        ~_lake_mask_pad, return_indices=True
                    )
                    _is_blend_cell_pad = (
                        _dist_from_lake_cap_pad <= _BLEND_PROTECT_DIST
                    )
                    # lake cells carry their flat water level in
                    # river_water_y (assigned in the lake_water_levels
                    # loop above) — nearest-lake level per cell.
                    _near_lake_y_pad = _river_water_y_pad[
                        _lk_idx_cap_pad[0], _lk_idx_cap_pad[1]
                    ]
                    _eff_cap_pad = np.where(
                        _is_blend_cell_pad,
                        np.maximum(_propagated_cap_pad, _near_lake_y_pad),
                        _propagated_cap_pad,
                    ).astype(np.int16)
                _too_high_pad = (
                    _river_cells_pad
                    & (_river_water_y_pad > _eff_cap_pad)
                    & (_eff_cap_pad > core_col_gen.SEA_LEVEL)
                )
                if _too_high_pad.any():
                    _river_water_y_pad[_too_high_pad] = _eff_cap_pad[_too_high_pad]
                    print(f"[s91-cap] tile=({tile_x},{tile_y}) capped "
                          f"{int(_too_high_pad.sum())} water cells at "
                          f"bank/lake level", file=sys.stderr, flush=True)

        # === S83 v13 PASS 0: CARVE COMPLETION (painted rivers only) ===
        # User v12 feedback: "the escape prevention surface trough wall
        # is not moving outwards to adjust to the changed riverbank, so
        # now there's a 'wall' with water on both sides in the riverbank."
        #
        # Root cause: v12's bed-override footprint was widened from
        # carve_depth > 0.5 to > 0.1 (in core/hydro_region_overlay.py),
        # extending the carved trough further laterally. The carver
        # writes water_y_field at all of those cells, BUT some boundary
        # cells end up with surface_y >= water_y after smoothing pulls
        # the bed up toward natural terrain. Those cells:
        #   - have river_water_y > SEA  (so escape-fix treats them as
        #     "water cells" and never raises surrounding land for them)
        #   - have surface >= water_y   (so MC shows no water at them)
        # Result: a dry strip down the middle of where water should be.
        #
        # Fix: BEFORE escape-fix runs, scan for cells with water_y > SEA
        # but surface >= water_y. Lower surface to (water_y - 1) so MC
        # shows water there. The subsequent escape-fix loop sees a clean
        # wide water mask and builds its wall at the true outer boundary.
        #
        # Gated on _paint_river_pad.any() — only fires for painted-river
        # tiles. Non-painted (WP-findPath) baseline rivers keep their
        # v8.14 behavior exactly (zero regression risk).
        if _paint_river_pad.any():
            _dry_water_pad = (
                (_river_water_y_pad > core_col_gen.SEA_LEVEL)
                & (_surface_y_pad >= _river_water_y_pad)
                & ~_lake_mask_pad
            )
            _n_dry = int(_dry_water_pad.sum())
            if _n_dry > 0:
                _surface_y_pad[_dry_water_pad] = (
                    _river_water_y_pad[_dry_water_pad] - np.int16(1)
                )
                print(f"[s83v13] tile=({tile_x},{tile_y}) carve_completion: "
                      f"lowered {_n_dry} dry-bed-in-water cells",
                      file=sys.stderr, flush=True)

            # S84 PASS 0.1 (flat Y=55 floor coastal hack) REMOVED.
            # Replaced by paint-always-carves in core/river_carver_v2.py:
            # the above_sea gate was dropped from river_channel and
            # river_full_mask construction so painted cells get the full
            # v17 spline+SDF+bed-cache treatment regardless of sea level.

        # === S83 v16 PASS 0.25: BED MELT AT 50K (painted rivers only) ===
        # User v15 feedback: "still flat bottoms at small channels" + "harsh
        # U-shape walls". Diagnosis: the 8k melt gaussian in
        # core/hydro_region_overlay.py operates on a sub-50k bed cache where
        # narrow channels (3-5 blocks wide at 50k = sub-pixel at 8k) aren't
        # represented — so the 8k bed cache has natural-terrain values for
        # those locations, the carver's np.minimum clamp picks the gravity-
        # carved floor (sharp smoothstep + flat plateau), and the 8k melt
        # achieves nothing useful for narrow features.
        #
        # Fix: run a 50k weighted gaussian on surface_y at water cells inside
        # this padded escape-fix block (PAD=48 already handles tile
        # boundaries). Weighted (water-only) so wide channels mostly average
        # with their own deep cells (preserves depth); narrow channels get
        # significant bank contribution through the weighted mean (shallowens
        # toward the bank, fixing the "bottom out" feel).
        #
        # Clamp: bed must stay <= water_y - 1 so water still shows above bed.
        # Gated on _paint_river_pad.any() — only fires for painted rivers,
        # zero risk of regression on WP-findPath baseline.
        if _paint_river_pad.any():
            from scipy.ndimage import gaussian_filter as _gf_bed_v16
            _BED_MELT_SIGMA_50K = 2.0  # S83 v17: 4 -> 2; smaller sigma preserves narrow-channel bowl variation
            _water_mask_bed_v16 = (
                (_river_water_y_pad > core_col_gen.SEA_LEVEL)
                & ~_lake_mask_pad
            )
            if _water_mask_bed_v16.any():
                _surface_f_v16 = _surface_y_pad.astype(np.float32)
                _w_v16 = _water_mask_bed_v16.astype(np.float32)
                # Weighted gaussian: water cells contribute to water cells
                # only (numerator) but normalize by gaussian-of-weights
                # (denominator) so cells near a narrow channel still get
                # a valid average (just from fewer water neighbors).
                _num_v16 = _gf_bed_v16(
                    _surface_f_v16 * _w_v16, sigma=_BED_MELT_SIGMA_50K)
                _den_v16 = _gf_bed_v16(_w_v16, sigma=_BED_MELT_SIGMA_50K)
                _eps_v16 = np.float32(1e-6)
                _bed_smooth_v16 = _num_v16 / (_den_v16 + _eps_v16)
                # Clamp: bed must stay strictly below water_y at water cells
                _water_y_f = _river_water_y_pad.astype(np.float32)
                _bed_smooth_v16 = np.minimum(
                    _bed_smooth_v16, _water_y_f - 1.0)
                # Apply only at water cells; everything else unchanged
                _new_surface_v16 = np.where(
                    _water_mask_bed_v16,
                    _bed_smooth_v16,
                    _surface_f_v16,
                ).astype(np.float32)
                # Count cells that meaningfully changed
                _n_bed_changed = int((
                    _water_mask_bed_v16
                    & (np.abs(_new_surface_v16 - _surface_f_v16) > 0.5)
                ).sum())
                _surface_y_pad = np.round(_new_surface_v16).astype(np.int16)
                if _n_bed_changed > 0:
                    print(f"[s83v16] tile=({tile_x},{tile_y}) "
                          f"bed_melt_50k: smoothed {_n_bed_changed} water "
                          f"cells (sigma={_BED_MELT_SIGMA_50K})",
                          file=sys.stderr, flush=True)

        # === S83 v15 PASS 0.5: BANK SMOOTHING (painted rivers only) ===
        # User direction: "smooth banks above water". Runs AFTER the
        # carve-completion pass (so dry-bed-in-water cells are already
        # corrected) and BEFORE the iterative escape-fix loop (so the
        # wall builder sees the smoothed bank silhouette).
        #
        # Approach: weighted gaussian on surface_y, weighted on cells that
        # are LAND (river_water_y < 0) AND within N blocks of river water.
        # Gaussian averages with bed cells (lower) and other bank cells
        # (similar), pulling the bank near water DOWN toward water level.
        # Clamps:
        #   - Never below SEA_LEVEL (don't carve into ocean territory)
        #   - Never below nearest water_y (don't sink land below water,
        #     which would just trigger escape-fix to raise it back)
        #   - Never above natural pre_carve_y (preserves natural bank
        #     silhouette where smoothing's averaging would otherwise raise)
        if _paint_river_pad.any():
            from scipy.ndimage import gaussian_filter as _gf_bank_v15
            from scipy.ndimage import distance_transform_edt as _edt_bank_v15
            _BANK_SMOOTH_RADIUS_BLOCKS = 12  # smooth bank cells within 12 of water
            _BANK_SMOOTH_SIGMA = 4.0          # decently sizeable, not massive
            _water_mask_bank = (
                _river_water_y_pad > core_col_gen.SEA_LEVEL)
            if _water_mask_bank.any():
                _dist_water_bank, _water_idx_bank = _edt_bank_v15(
                    ~_water_mask_bank, return_indices=True)
                _is_land_bank = _river_water_y_pad < 0
                _bank_zone = (
                    _is_land_bank
                    & (_dist_water_bank <= _BANK_SMOOTH_RADIUS_BLOCKS)
                )
                if _bank_zone.any():
                    _surface_f = _surface_y_pad.astype(np.float32)
                    _surface_smoothed = _gf_bank_v15(
                        _surface_f, sigma=_BANK_SMOOTH_SIGMA)
                    # Nearest water_y for clamp lower bound
                    _nearest_wy_bank = _river_water_y_pad[
                        _water_idx_bank[0], _water_idx_bank[1]
                    ].astype(np.float32)
                    # Build target: only LOWER (gaussian-smoothed) at bank cells
                    _bank_target = np.minimum(_surface_smoothed, _surface_f)
                    # Clamp: never below nearest water level
                    _bank_target = np.maximum(_bank_target, _nearest_wy_bank)
                    # Clamp: never below SEA_LEVEL
                    _bank_target = np.maximum(
                        _bank_target,
                        np.float32(core_col_gen.SEA_LEVEL))
                    # Apply only at bank zone cells
                    _new_surface_bank = np.where(
                        _bank_zone, _bank_target, _surface_f
                    ).astype(np.float32)
                    _n_bank_changed = int((
                        _bank_zone & (_new_surface_bank < _surface_f - 0.5)
                    ).sum())
                    _surface_y_pad = np.round(
                        _new_surface_bank).astype(np.int16)
                    if _n_bank_changed > 0:
                        print(f"[s83v15] tile=({tile_x},{tile_y}) "
                              f"bank_smooth: lowered {_n_bank_changed} "
                              f"bank cells (sigma={_BANK_SMOOTH_SIGMA}, "
                              f"radius={_BANK_SMOOTH_RADIUS_BLOCKS}b)",
                              file=sys.stderr, flush=True)

        _s9_snap("p05_smooth", _surface_y_pad)

        # (Pass 0.05 water-level cap runs at the TOP of this padded block —
        # see above. v4 bisect proof it must precede the v15 bank smooth:
        # bank smooth's "never below nearest water" clamp RAISES banks to
        # the uncapped water level, building the levee before any
        # containment pass runs.)

        _s9_snap("p07_cap", _surface_y_pad)
        if _s9_bisect:
            np.save(f"{_s9_bisect}/snap_rwy07_{tile_x}_{tile_y}.npy",
                    _river_water_y_pad[_PAD:_PAD + _H, _PAD:_PAD + _W])

        # ── Pass 1: escape-fix on padded ──
        from scipy.ndimage import maximum_filter as _maxf_escape
        from scipy.ndimage import distance_transform_edt as _edt_berm
        # Treat -999 (no water) as 0 so it doesn't dominate the max.
        _water_y_positive_pad = np.where(
            _river_water_y_pad > 0, _river_water_y_pad, np.int16(0)
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
            _nbr_max_wy_pad = _maxf_escape(_water_y_positive_pad, size=3)
            _leak_cells_pad = (
                (_surface_y_pad < _nbr_max_wy_pad)
                & (_nbr_max_wy_pad > core_col_gen.SEA_LEVEL)
                & (_river_water_y_pad < 0)  # land only
            )
            if not _leak_cells_pad.any():
                break
            _surface_y_pad[_leak_cells_pad] = _nbr_max_wy_pad[_leak_cells_pad]

        _s9_snap("p1_escape", _surface_y_pad)

        # ── Pass 2: EDT berm on padded ──
        # S81 v8.8: EDT-based smooth-slope berm. Slope:
        #   dist=1 → water_y - 1, dist=2 → water_y - 2, … dist=8 → water_y - 8
        # The escape-fix above already pinned dist=1 to water_y; this
        # berm only raises cells beyond that.
        BERM_RADIUS = 8
        _water_mask_for_berm_pad = (_river_water_y_pad > core_col_gen.SEA_LEVEL)
        if _water_mask_for_berm_pad.any():
            _dist_from_water_pad, _water_idx_pad = _edt_berm(
                ~_water_mask_for_berm_pad, return_indices=True
            )
            _nearest_water_y_pad = _river_water_y_pad[
                _water_idx_pad[0], _water_idx_pad[1]
            ]
            _berm_target_pad = (
                _nearest_water_y_pad.astype(np.int16)
                - _dist_from_water_pad.astype(np.int16)
            )
            _need_berm_pad = (
                (_surface_y_pad < _berm_target_pad)
                & (_berm_target_pad > core_col_gen.SEA_LEVEL)
                & (_river_water_y_pad < 0)
                & (_dist_from_water_pad <= BERM_RADIUS)
            )
            if _need_berm_pad.any():
                _surface_y_pad[_need_berm_pad] = _berm_target_pad[_need_berm_pad]

        _s9_snap("p2_berm", _surface_y_pad)

        # ── Pass 2b: S91 regression #5 — grade the lakeshore berm ──
        # Pass 1 (escape-fix) + Pass 2 (EDT berm) build a 1-px-slope
        # containment cone wherever water sits above adjacent land. In the
        # v8.12 lake BLEND zone river water_y is deliberately raised toward
        # the lake level, so around lakes the cone reads as an engineered
        # RETAINING WALL — higher than both the water and the natural land
        # behind it — with the un-raised ground beyond it reading as a dry
        # TRENCH/swale (S90 walk regression #5; reproduced at (19,76):
        # 2496 Step-9-raised cells hugging river cells, 104/119 trench
        # cells adjacent to a wall). Ocean beaches are untouched (the
        # berm_target > SEA_LEVEL gate already blocks sea-level berms).
        #
        # Grade: masked-normalized gaussian over LAND heights in a band
        # around the lake, blend the band toward it (dissolves the cone
        # into a wide natural bank + part-fills the swale edge), then
        # RE-RUN the escape loop so any graded cell that re-opened a leak
        # is re-pinned at water level (a 1-px lip, not a cone). Runs on
        # the PADDED arrays -> seam-safe. Radius MUST stay <= 14: beyond
        # that, land is outside the post-decorate restore's water zone
        # (restored -> grade discarded) and outside the schematic water
        # buffer (trees anchored there would float over graded ground).
        _lg_cfg = (cfg.get("hydrology_engine", {})
                   .get("river_geometry", {}).get("lake_carve", {}))
        _LG_RADIUS = int(_lg_cfg.get("rim_grade_radius_px", 12))
        _LG_SIGMA = float(_lg_cfg.get("rim_grade_sigma", 4.0))
        # S93e4: the rim grade also bands ABOVE-SEA RIVER water. The
        # taper narrowed channels to a tube, so the elevation change that
        # the old 40-wide carve spread across gentle banks now happened
        # in ~2 cells at the tube edge — vertical cut walls, banks p50 7
        # blocks above water at 14 px out on (30,12) (user: "I see
        # trenches"). Same masked gaussian + leak re-pin; sea-level water
        # excluded (estuary fans/beaches untouched). Knob
        # river_carve.headwater_taper.rim_grade (default on).
        # S93e5: default OFF — with slope-adaptive water the stream hugs
        # the hillside (no walls to grade), and the grade's re-dressed
        # dirt band was part of the user's trench complaint.
        _rg_on = bool((cfg.get("river_carve", {})
                       .get("headwater_taper", {})).get("rim_grade", False))
        _wet_lg = _lake_mask_pad
        if _rg_on:
            _wet_lg = _wet_lg | (
                _water_y_positive_pad > core_col_gen.SEA_LEVEL)
        if _LG_RADIUS > 0 and _wet_lg.any():
            from scipy.ndimage import binary_dilation as _bd_lg
            from scipy.ndimage import gaussian_filter as _gf_lg
            _land_pad_lg = _river_water_y_pad < 0
            _band_lg = (_bd_lg(_wet_lg, iterations=_LG_RADIUS)
                        & _land_pad_lg)
            if _band_lg.any():
                # masked-normalized gaussian over LAND ONLY — water/bed
                # heights must not bleed into the bank average.
                _w_lg = _land_pad_lg.astype(np.float32)
                _num_lg = _gf_lg(
                    _surface_y_pad.astype(np.float32) * _w_lg, sigma=_LG_SIGMA)
                _den_lg = _gf_lg(_w_lg, sigma=_LG_SIGMA)
                _sm_lg = np.where(
                    _den_lg > 1e-3, _num_lg / (_den_lg + 1e-6),
                    _surface_y_pad.astype(np.float32))
                _graded_lg = np.where(
                    _band_lg, np.round(_sm_lg),
                    _surface_y_pad.astype(np.float32)).astype(np.int16)
                _n_lg = int((_graded_lg != _surface_y_pad).sum())
                if _n_lg:
                    _surface_y_pad = _graded_lg
                    # re-pin leaks the grade may have re-opened (verbatim
                    # Pass-1 escape loop; water_y arrays are unchanged).
                    for _lg_iter in range(5):
                        _nbr_max_lg = _maxf_escape(_water_y_positive_pad, size=3)
                        _leak_lg = (
                            (_surface_y_pad < _nbr_max_lg)
                            & (_nbr_max_lg > core_col_gen.SEA_LEVEL)
                            & (_river_water_y_pad < 0)
                        )
                        if not _leak_lg.any():
                            break
                        _surface_y_pad[_leak_lg] = _nbr_max_lg[_leak_lg]
                    print(f"[s91-rimgrade] tile=({tile_x},{tile_y}) graded "
                          f"{_n_lg} lakeshore cells (r={_LG_RADIUS}, "
                          f"sigma={_LG_SIGMA})", file=sys.stderr, flush=True)

        # (Pass 3 / v8.14 water-level cap MOVED to Pass 0.7, BEFORE the
        # escape-fix + berm — S91 #5 order fix. Capping after containment
        # left orphaned levees: terrain raised in Pass 1/2 for high water
        # that the cap then lowered.)
        _s9_snap("p2b_grade", _surface_y_pad)

        # ── S94 FLOOD-SETTLE (config river_carve.flood_settle) ─────────────
        # USER (84,60 walk): "SAME terraces across the entire latitude band —
        # no split down the middle", via the MC glass-platform trick simulated
        # in-pipeline. The v8.14 cap (Pass 0.05) does water = min(clean,
        # eff_cap) with eff_cap = the per-cell propagated NEAREST-EDGE bank —
        # so on a wide channel the LEFT and RIGHT banks differ and the two
        # halves settle to different levels: a 1-block ledge running LENGTHWISE
        # down the channel centre. Replace that per-cell cap with a flood-
        # settle that SUPERSEDES it: take the CLEAN carver source (the flat
        # platform sheet), then per true cross-section (nearest-centerline
        # slice) settle to min(source, lowest lateral bank) — flat per latitude
        # band, contained by construction (water can never sit above a bank =>
        # ZERO levees, the spill drops it), and enforce monotone non-increasing
        # toward the ocean so it only steps DOWN. The lumpy BED is never
        # touched (water-only); where a high bed bump exceeds the contained
        # level it pokes through as a rock. Runs on the PADDED halo so a river
        # crossing a seam settles identically on both sides. Flow coordinate =
        # a local dist-from-ocean (ocean = surface<=SEA, else seed lowest
        # source cell — mirrors the carver's _dist_from_ocean). Centerline =
        # skeletonize of the WETTED source mask (NOT _river_cells_pad, which
        # includes Step-8 bank dilation and skeletonizes branchy).
        _fs_cfg = (cfg.get("river_carve", {}) or {}).get("flood_settle", {}) \
            if isinstance(cfg, dict) else {}
        if bool(_fs_cfg.get("enabled", False)) and _river_cells_pad.any():
            _SEA = core_col_gen.SEA_LEVEL
            _src_pad = _clean_water_pad_s94.astype(np.int32)
            _riv_w = (_river_cells_pad & (_src_pad > _SEA) & ~_lake_mask_pad)
            if _riv_w.sum() >= 8:
                from skimage.morphology import skeletonize as _skz_fs
                from scipy.ndimage import distance_transform_edt as _edt_fs
                try:
                    _skel_fs = _skz_fs(_riv_w)
                except Exception:
                    _skel_fs = _riv_w
                if not _skel_fs.any():
                    _skel_fs = _riv_w
                # local dist-from-ocean (lower = downstream)
                _ocean_fs = _surface_y_pad <= _SEA
                if not _ocean_fs.any():
                    _rr_fs, _cc_fs = np.where(_riv_w)
                    _lo_fs = int(np.argmin(_surface_y_pad[_rr_fs, _cc_fs]))
                    _ocean_fs = np.zeros_like(_riv_w)
                    _ocean_fs[_rr_fs[_lo_fs], _cc_fs[_lo_fs]] = True
                _dist_fs = _edt_fs(~_ocean_fs).astype(np.float32)
                # true-land bank mask EXCLUDES lakes (a lake is water, not a
                # bank — else a river beside a lake drains to the lake bed).
                _land_fs = ~_river_cells_pad & ~_lake_mask_pad
                _settled = core_river_settle.settle(
                    source=_src_pad, bed=_surface_y_pad.astype(np.int32),
                    river=_riv_w, dist=_dist_fs, skel=_skel_fs, land=_land_fs)
                # S94 seam-walk: gated dump of the flood-settle PADDED inputs +
                # output so a harness can run settle() variants offline and
                # check seam continuity without a 25-min re-render per variant.
                _fd = os.environ.get("FLOOD_DUMP_DIR")
                if _fd:
                    os.makedirs(_fd, exist_ok=True)
                    for _nm, _ar in (("src", _src_pad), ("bed", _surface_y_pad),
                                     ("riv", _riv_w), ("dist", _dist_fs),
                                     ("skel", _skel_fs), ("land", _land_fs),
                                     ("settled", _settled)):
                        np.save(f"{_fd}/fs_{_nm}_{tile_x}_{tile_y}.npy",
                                np.asarray(_ar))
                _wmask = _riv_w & (_settled > _SEA)
                _river_water_y_pad[_wmask] = _settled[_wmask].astype(np.int16)
                print(f"[s94-flood] tile=({tile_x},{tile_y}) flood-settled "
                      f"{int(_riv_w.sum())} river cells",
                      file=sys.stderr, flush=True)

                # ── S94 GLOBAL river water-level override (the seam fix) ──────
                # The per-tile settle above is now the FALLBACK. If the global
                # hydro_river_wl bake exists, OVERRIDE the water level with it on
                # covered river cells. It is one global field, so the value at
                # any world coord is identical on both sides of a tile seam ->
                # the 1-block water-level seam step is gone. The river EXTENT
                # stays per-tile (chunk_writer fills where level > bed at full
                # res) so the perimeter stays organic (no NEAREST swimming-pool
                # geometry). Read the PADDED window (boundless, -999 fill) so the
                # bank-taper that follows is seam-consistent too.
                _grwl_path = masks_dir / "hydro_river_wl.tif"
                if _grwl_path.exists():
                    try:
                        import rasterio as _rio_wl
                        from rasterio.windows import Window as _Win
                        with _rio_wl.open(_grwl_path) as _wls:
                            _gll = _wls.read(
                                1, window=_Win(col_off - _PAD, row_off - _PAD,
                                               _PW, _PH),
                                boundless=True, fill_value=-999,
                            ).astype(np.int16)
                        _ov = _riv_w & (_gll > _SEA)
                        _river_water_y_pad[_ov] = _gll[_ov]
                        print(f"[s94-river-wl] tile=({tile_x},{tile_y}) global "
                              f"level override on {int(_ov.sum())}/"
                              f"{int(_riv_w.sum())} river cells",
                              file=sys.stderr, flush=True)
                    except Exception as _wl_exc:  # noqa: BLE001
                        print(f"[s94-river-wl] WARN tile=({tile_x},{tile_y}) "
                              f"{type(_wl_exc).__name__}: {_wl_exc}",
                              file=sys.stderr, flush=True)

        # ── S94 BANK TAPER (config river_carve.bank_taper) ─────────────────
        # USER (84,60 walk): the flood-settled water sits in abrupt TROUGH
        # WALLS — taper them into a natural gaussian valley. Spec: the 1-cell
        # LAND perimeter touching water is flush at water level W; the next
        # ring out is W+1 (secondary containment wall); beyond, a constant-
        # gentle-grade ramp (reach SCALES WITH WALL HEIGHT) gaussian-smoothed
        # to the natural terrain. Only LOWERS bank cells (never raises, never
        # touches the bed/water/emergent-rocks); terrace-safe (per-cell
        # nearest-water W + a highest-nearby-pool floor so no terrace drains).
        # Runs on the PADDED surface (seam-safe halo) right after the flood-
        # settle, before the crop. The later rock re-lock SKIPS river banks so
        # it won't fight this; it is the final say on bank Y.
        _bt_cfg = (cfg.get("river_carve", {}) or {}).get("bank_taper", {}) \
            if isinstance(cfg, dict) else {}
        if (bool(_bt_cfg.get("enabled", False)) and _river_cells_pad.any()):
            _rm_pad_bt = np.zeros(_surface_y_pad.shape, dtype=np.uint8)
            _rm_pad_bt[_river_cells_pad] = 1
            _rm_pad_bt[_lake_mask_pad] = 3
            _tapered = core_bank_taper.taper(
                _surface_y_pad, _river_water_y_pad, _rm_pad_bt)
            _ch_bt = int((_tapered.astype(np.int32)
                          != _surface_y_pad.astype(np.int32)).sum())
            _surface_y_pad[:, :] = _tapered.astype(_surface_y_pad.dtype)
            print(f"[s94-banktaper] tile=({tile_x},{tile_y}) tapered "
                  f"{_ch_bt} bank cells", file=sys.stderr, flush=True)

            # S94: despike thin 'stonehenge' emergent-rock columns (the bank-
            # taper skips river-footprint cells, so isolated 1-wide tall rock
            # pixels there are never smoothed). Lowers only THIN tall rock
            # components; broad outcrops kept. Runs on the same padded surface.
            if bool(_bt_cfg.get("despike_rock", True)):
                _desp = core_bank_taper.despike_emergent_rock(
                    _surface_y_pad, _river_water_y_pad, _rm_pad_bt)
                _ch_ds = int((_desp.astype(np.int32)
                              != _surface_y_pad.astype(np.int32)).sum())
                _surface_y_pad[:, :] = _desp.astype(_surface_y_pad.dtype)
                if _ch_ds:
                    print(f"[s94-despike] tile=({tile_x},{tile_y}) lowered "
                          f"{_ch_ds} thin rock-column cells",
                          file=sys.stderr, flush=True)

        # ── S94 SPILL-ROW CLEANUP (user): the seam-clean GLOBAL water level is
        # correct except where it sits above the local terrain -> spills over a
        # lower bank. Instead of containing it (wall-or-seam), DELETE the spilling
        # water: any widthwise row of surface water whose EDGE block is exposed to
        # air (land just beyond it is below the water surface) is replaced with
        # air. The perching slices vanish; the contained river stays. Runs on the
        # PADDED surface (seam-safe halo) after all bank shaping. Terrain-driven
        # => both tiles delete the same rows at a seam.
        if _river_cells_pad.any():
            _rm_cln = (_river_cells_pad & ~_lake_mask_pad)
            _wy_cln, _del_cln = core_water_cleanup.cleanup_spill_rows(
                _river_water_y_pad, _surface_y_pad, _rm_cln)
            _nd = int(_del_cln.sum())
            if _nd:
                _river_water_y_pad[:, :] = _wy_cln.astype(_river_water_y_pad.dtype)
                print(f"[s94-spill-cleanup] tile=({tile_x},{tile_y}) lowered "
                      f"{_nd} overspilled water cells", file=sys.stderr, flush=True)

        # ── S94c FILL-TO-BANKS (realism: "steep banks nearly all dry"): the
        # bank-taper lowers the valley floor BELOW the contained channel water
        # surface, but the water mask is the narrow carved channel -> the tapered
        # bench renders DRY below the waterline. Flood water OUTWARD to every dry
        # cell sitting below an adjacent water level, bounded where terrain rises
        # to the bank. NO terrain raised (no walls). PADDED -> seam-safe. Requires
        # the level be CONTAINED (rebuild_river_wl --bank-cover) else it pours over
        # the bank; runs AFTER the spill-cleanup so it fills to the contained level.
        if _river_cells_pad.any():
            _wy_fill, _filled = core_water_fill.fill_to_banks(
                _river_water_y_pad, _surface_y_pad, _lake_mask_pad)
            _nf = int(_filled.sum())
            if _nf:
                _river_water_y_pad[:, :] = _wy_fill.astype(_river_water_y_pad.dtype)
                print(f"[s94-fill-banks] tile=({tile_x},{tile_y}) filled "
                      f"{_nf} sub-level bank cells to the waterline",
                      file=sys.stderr, flush=True)

        # ── Crop padded results back to inner tile ──
        surface_y[:, :] = _surface_y_pad[_PAD:_PAD + _H, _PAD:_PAD + _W]
        river_water_y[:, :] = _river_water_y_pad[_PAD:_PAD + _H, _PAD:_PAD + _W]

        # S87 walk #3 Phase 2A re-lock: restore rock-pixel Y to the displaced
        # snapshot from Step 6e.  Step 9's gaussian smoothing + water/lake
        # fixes can perturb surface_y at rock pixels; the lock ensures the
        # final Y matches what schematic placement (Step 8) saw.  Without
        # this, trees float and trunk extension fires to MAX_TRUNK_EXT=6.
        # S87 walk #4 v4: DEFENSIVE -- skip any pixel within river bank
        # dilation zone, even if the river_fade should have already zeroed
        # them out.  User reported "dry staircased river" regression -- the
        # lock was likely stomping Step 9's WP-style water-bank lowering.
        if _crunch_lock_y is not None and _crunch_rock_mask is not None:
            _final_mask = _crunch_rock_mask
            # S94c: protect FILL-TO-BANKS water (river_water_y>63) too, so the
            # re-lock doesn't raise terrain back up through the newly-filled bank.
            _riv_or_fill = (((river_meta > 0) if river_meta is not None
                             else np.zeros_like(surface_y, dtype=bool))
                            | (river_water_y > 63))
            if _riv_or_fill.any():
                from scipy.ndimage import binary_dilation as _bd_lock
                _river_zone_lock = _bd_lock(_riv_or_fill, iterations=8)
                _final_mask = _crunch_rock_mask & ~_river_zone_lock
                del _river_zone_lock
            surface_y[_final_mask] = _crunch_lock_y[_final_mask]

        # S89 floating-tree lock: restore the post-decorate surface_y on LAND
        # cells away from water so chunk_writer's columns match the schematic
        # anchors (Step 9 only legitimately reshapes water/bank cells; restoring
        # land there is safe and stops trunks floating).  Exclude a dilated water
        # zone so river/lake bank smoothing survives.
        if _post_decorate_y is not None:
            _land_lock = surface_y >= 63
            # S94c: include FILL-TO-BANKS water (river_water_y>63) in the water
            # zone so restoring land doesn't undo the tapered+flooded bank.
            _wz_src = (((river_meta > 0) if river_meta is not None
                        else np.zeros_like(surface_y, dtype=bool))
                       | (surface_y < 63) | (river_water_y > 63))
            if _wz_src.any():
                from scipy.ndimage import binary_dilation as _bd_lk2
                _water_zone = _bd_lk2(_wz_src, iterations=14)
                _land_lock &= ~_water_zone
                del _water_zone
            surface_y[_land_lock] = _post_decorate_y[_land_lock]

        # S89-walk4: bowl-carve lake-bed RE-LOCK (final say). Restore the smooth
        # carved bowl bed for lake cells, overriding the post-carve flow-erosion +
        # smoothing passes that were refilling the shallow walls (antipattern #2).
        # Only lowers (min) so we never RAISE a bed a later pass legitimately
        # deepened; lake cells are underwater so this is purely the bowl floor.
        if _lake_bed_lock_y is not None and _lake_bed_lock_mask is not None:
            _lk = _lake_bed_lock_mask
            surface_y[_lk] = np.minimum(surface_y[_lk], _lake_bed_lock_y[_lk])

        # NOTE: a post-relock "Pass 2" cleanup was tried and REVERTED — the final
        # re-locks raise some beds ABOVE their bank, so trimming there lands the
        # water below the raised bed (dry) and cascades: 13,80 went 10111/131 ->
        # 9740/180 (worse). Post-relock trimming drains, it does not contain. The
        # 131 residual on 13,80 is the GLOBAL bake over-leveling that tile's
        # interior; the cure is the bake, not more trimming. See S94 handoff.

        # S94 Phase 2: paint EMERGENT river-bed shoals (the rocks left by the
        # flood-settle where the lumpy bed pokes above the contained water) as
        # the per-cell lithology DARK band, so they read as natural rock not
        # the decorated dirt/grass surface. Exposed = river cell tagged with
        # above-sea water but where surface_y >= river_water_y (chunk_writer
        # places NO water there). Lakes excluded. surface_y + river_water_y are
        # final here (post all Step-9 locks). Gated with flood_settle.
        _fs_cfg2 = (cfg.get("river_carve", {}) or {}).get("flood_settle", {}) \
            if isinstance(cfg, dict) else {}
        if (bool(_fs_cfg2.get("enabled", False))
                and bool(_fs_cfg2.get("paint_rocks", True))):
            _SEA2 = core_col_gen.SEA_LEVEL
            _CHAN_LAKE2 = np.uint8(3)
            _exposed = ((river_meta > 0) & (river_meta != _CHAN_LAKE2)
                        & (river_water_y > _SEA2)
                        & (surface_y >= river_water_y))
            if _exposed.any():
                _np = core_decorator.paint_river_rocks(
                    surface_blk, sub_blk, lithology_tile, _exposed,
                    cfg, tile_x, tile_y)
                print(f"[s94-rocks] tile=({tile_x},{tile_y}) painted "
                      f"{_np}/{int(_exposed.sum())} emergent river rocks as "
                      f"litho dark band", file=sys.stderr, flush=True)

        # S91 #5 diag: env-gated FINAL-surface dump (post Step-9 water fixes +
        # all locks).  The dry-run SURF_DUMP hook never sees these passes —
        # this is the only way to inspect the exact surface chunk_writer gets
        # without reading the .mca back.
        import os as _os_s9d
        _s9_dump = _os_s9d.environ.get("SURF_DUMP_STEP9_DIR")
        if _s9_dump:
            _os_s9d.makedirs(_s9_dump, exist_ok=True)
            np.save(f"{_s9_dump}/sy_final_{tile_x}_{tile_y}.npy", surface_y)
            np.save(f"{_s9_dump}/rwy_{tile_x}_{tile_y}.npy", river_water_y)
            np.save(f"{_s9_dump}/rmeta9_{tile_x}_{tile_y}.npy", river_meta)
            if _post_decorate_y is not None:
                np.save(f"{_s9_dump}/sy_postdec_{tile_x}_{tile_y}.npy",
                        _post_decorate_y)

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
            gap_mask     = eco_grads.gap_mask if eco_grads is not None else None,
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
    # with them.  S85: assets/vandir_height.zip is the 768-block-height
    # version (Y -64 to Y 703, 48 sections).  Vanilla MC 1.21.10 supports
    # only 24 sections without this datapack — chunks crash with
    # ArrayIndexOutOfBoundsException on load.
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
