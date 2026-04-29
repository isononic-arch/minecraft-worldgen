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
            for lid in range(1, n_lakes + 1):
                lk = lake_labeled == lid
                # Lake water level = lowest shore point (min pre_carve_y in lake) - 1.
                lake_water = int(pre_carve_y[lk].min()) - 1
                lake_water_levels[lid] = np.int16(lake_water)
                # Set water Y for all lake pixels (carved or not)
                river_water_y[lk] = np.int16(lake_water)

            # Connectivity channels: ensure continuous water end-to-end.
            # Each channel connects a lake to a river (or river to river
            # via a lake).  Water level = shallowest endpoint so water
            # flows naturally downhill through the channel.
            CHAN_STREAM = np.uint8(1)
            if conn_channel_mask.any():
                from scipy.ndimage import label as _label_conn
                from scipy.ndimage import maximum_filter as _maxf_ch
                from scipy.ndimage import distance_transform_edt as _edt_conn

                # Label each separate connectivity channel
                conn_labeled, n_conn = _label_conn(conn_channel_mask)

                # For each channel, find the lake and river it touches
                # and set water level to the shallowest endpoint.
                dist_to_lake = _edt_conn(~lake_mask).astype(np.float32)
                river_or_stream = (river_meta == CHAN_RIVER) | (river_meta == CHAN_STREAM)
                # "Original" river = river pixels that aren't connectivity channels
                orig_river = river_or_stream & ~conn_channel_mask
                dist_to_river = _edt_conn(~orig_river).astype(np.float32) if orig_river.any() else np.full_like(dist_to_lake, 9999)

                # Expand lake labels so we can find which lake each channel touches
                expanded_lake_labels = _maxf_ch(lake_labeled, size=7)

                for cid in range(1, n_conn + 1):
                    ch = conn_labeled == cid
                    if ch.sum() < 2:
                        continue

                    # Find the lake this channel connects to
                    ch_lake_ids = expanded_lake_labels[ch]
                    ch_lake_ids = ch_lake_ids[ch_lake_ids > 0]
                    if ch_lake_ids.size > 0:
                        lake_id = int(np.bincount(ch_lake_ids).argmax())
                        lake_wl = int(lake_water_levels[lake_id])
                    else:
                        # No lake found — use the lowest point on the channel
                        lake_wl = int(pre_carve_y[ch].min()) - 1

                    # Find the river water level at the river-side endpoint
                    ch_river_wy = river_water_y[ch & orig_river]
                    if ch_river_wy.size > 0:
                        river_wl = int(ch_river_wy[ch_river_wy > -999].max()) if (ch_river_wy > -999).any() else lake_wl
                    else:
                        # Channel doesn't overlap original river — check
                        # the nearest river pixel's water level
                        ch_rows, ch_cols = np.where(ch)
                        ch_rdist = dist_to_river[ch_rows, ch_cols]
                        nearest_idx = np.argmin(ch_rdist)
                        nr, nc = int(ch_rows[nearest_idx]), int(ch_cols[nearest_idx])
                        # Walk toward nearest river pixel
                        river_wl = int(pre_carve_y[nr, nc]) - 1

                    # Water level = shallowest (highest Y) endpoint
                    channel_wl = np.int16(max(lake_wl, river_wl))

                    # Carve floor below water level so water fills end-to-end
                    too_high = ch & (surface_y >= channel_wl)
                    surface_y[too_high] = np.int16(channel_wl - 1)

                    # Set water level for the whole channel
                    river_water_y[ch] = channel_wl

            # Blend river water level toward lake level at river-lake interfaces.
            # River pixels near a lake adopt the lake's flat water Y, tapering
            # back to per-pixel terrain-following over BLEND_DIST blocks.
            BLEND_DIST = 8
            river_carved = (river_meta == CHAN_RIVER) & carved
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
