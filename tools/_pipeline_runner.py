"""
tools/_pipeline_runner.py — Pre-MCA pipeline runner for validators

Extracts the "run pipeline up to surface decoration" sequence from
validate_test_tile.py so validate_3x3.py can reuse it without running
chunk_writer or schematic placement (the two expensive steps).

This module is SHARED by validate_test_tile.py and validate_3x3.py.
Do NOT import PyQt6 here. Do NOT write .mca here.

Public API:
    run_tile_prelude(tx, tz, cfg, masks_dir, cfg_path, *, place_schematics=False)
        -> TileArtifacts

TileArtifacts is a dataclass holding every array downstream checks need:
    tile_x, tile_z         — tile coords
    masks                  — raw masks dict from tile_streamer.read_tile
    biome_grid             — (512, 512) object array of Vandir biome names
    surface_y              — (512, 512) int16 array of final surface Y (post-carve)
    pre_carve_y            — (512, 512) int16 array before river carving
    river_meta             — (512, 512) uint8 channel meta from river_carver_v2
    eco_grads              — EcoGradients namedtuple-like object or None
    cliff_deg              — (512, 512) float32 degrees
    surface_blk            — (512, 512) object array of surface block names
    sub_blk                — (512, 512) object array
    ground_cover           — (512, 512) object array
    col_results            — list-of-lists of ColumnResult (for column_profile/water_fill checks)
    placements             — list of Placement (empty if place_schematics=False)
    elapsed_ms             — int wall time for this tile

The runner is designed to be importable and callable N times with shared
process state — each call still reallocates arrays (the col_results grid
and column_generator's internal buffers are the expensive bit), but it
skips chunk_writer and schematic placement entirely unless place_schematics
is True.
"""
from __future__ import annotations

import importlib
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np


TILE_SIZE = 512


@dataclass
class TileArtifacts:
    tile_x: int
    tile_z: int
    masks: dict[str, np.ndarray]
    biome_grid: np.ndarray
    surface_y: np.ndarray
    pre_carve_y: np.ndarray
    river_meta: np.ndarray
    eco_grads: Any
    cliff_deg: np.ndarray
    surface_blk: np.ndarray
    sub_blk: np.ndarray
    ground_cover: np.ndarray
    col_results: list
    lithology_tile: np.ndarray | None = None  # (64, 64) uint8 at 1:8 — Phase 1.75
    placements: list = field(default_factory=list)
    elapsed_ms: int = 0


# MC biome mapping — canonical source is core/chunk_writer.BIOME_TO_MC
# Imported lazily in _mc_biome_map() to avoid circular imports at module level.


_core_cache: dict[str, Any] = {}


def _import_core() -> dict[str, Any]:
    """Cache-lazy import of all core modules the runner needs."""
    if _core_cache:
        return _core_cache

    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    names = [
        "core.biome_assignment",
        "core.tile_streamer",
        "core.column_generator",
        "core.river_carver_v2",
        "core.surface_decorator",
        "core.schematic_placement",
        "core.schematic_loader",
        "core.noise_fields",
        "core.eco_gradients",
    ]
    for n in names:
        _core_cache[n] = importlib.import_module(n)
    return _core_cache


def _mc_biome_map(biome_grid: np.ndarray) -> np.ndarray:
    from core.chunk_writer import BIOME_TO_MC
    mc_biomes = np.empty(biome_grid.shape, dtype=object)
    for b in np.unique(biome_grid):
        mc_biomes[biome_grid == b] = BIOME_TO_MC.get(str(b), BIOME_TO_MC["_DEFAULT"])
    return mc_biomes


def run_tile_prelude(
    tile_x: int,
    tile_z: int,
    cfg: dict,
    masks_dir: Path,
    cfg_path: Path,
    *,
    place_schematics: bool = False,
    schem_index_path: Optional[Path] = None,
    verbose: bool = False,
) -> TileArtifacts:
    """
    Run one tile through mask read → biome assignment → column generation →
    river carving → eco gradients → surface decoration.

    Stops before chunk_writer.write_tile(). Optionally runs schematic
    placement (expensive, default off).

    Raises RuntimeError on fatal pipeline failures. Caller should catch
    and convert to a FAIL check.
    """
    t0 = time.perf_counter()
    core = _import_core()
    core_biome   = core["core.biome_assignment"]
    core_tiles   = core["core.tile_streamer"]
    core_col     = core["core.column_generator"]
    core_river   = core["core.river_carver_v2"]
    core_dec     = core["core.surface_decorator"]
    core_place   = core["core.schematic_placement"]
    core_schem   = core["core.schematic_loader"]
    core_noise   = core["core.noise_fields"]
    core_eco     = core["core.eco_gradients"]

    def _log(msg: str) -> None:
        if verbose:
            print(f"[runner] ({tile_x},{tile_z}) {msg}", file=sys.stderr)

    col_off = tile_x * TILE_SIZE
    row_off = tile_z * TILE_SIZE

    # ---- Step 4: read masks ----
    _log("read_tile")
    # S60: build query-time gap config so rock_gap / snow_gap are sampled live
    # from the 8k Gaea sources via Catmull-Rom instead of the 50k TIFs.
    from core.gaea_gap_sampler import build_gap_config as _build_gap_cfg
    _gap_cfg = _build_gap_cfg(cfg.get("gaea_gaps", {}), masks_dir)
    masks = core_tiles.read_tile(
        masks_dir=masks_dir, col_off=col_off, row_off=row_off,
        width=TILE_SIZE, height=TILE_SIZE,
        gap_config=_gap_cfg,
    )

    # S70: Apply hydro_region.png paint overlay (lakes + rivers).  No-op
    # when the file is absent.  Mutates masks in place before river_carver.
    from core.hydro_region_overlay import apply_hydro_region_overlay
    apply_hydro_region_overlay(
        masks, masks_dir, col_off, row_off, TILE_SIZE, verbose=verbose,
    )

    noise = core_noise.load_noise_generators(cfg_path)

    # ---- Step 4a: read discrete lithology mask (Phase 1.75) ----
    # lithology.tif is 6250×6250 (1:8 scale) — read at 1:8 coords.
    # _fill_geology_layers() handles upscale 64→512 via NEAREST zoom.
    _lith_col = col_off // 8
    _lith_row = row_off // 8
    _lith_w   = max(1, TILE_SIZE // 8)
    _lith_h   = max(1, TILE_SIZE // 8)
    lithology_tile = core_tiles.read_discrete_tile(
        masks_dir / "lithology.tif", _lith_col, _lith_row,
        width=_lith_w, height=_lith_h,
    )

    # ---- Step 5: biome assignment ----
    _log("assign_biomes")
    biome_grid = core_biome.assign_biomes(
        height_tile=masks["height"], slope_tile=masks["slope"],
        flow_tile=masks["flow"],     erosion_tile=masks["erosion"],
        override_tile=masks["override"], noise_fields=noise, cfg=cfg,
    )

    # ---- Step 6: column generation ----
    _log("process_tile_columns_v2")
    h_u16  = (masks["height"]  * 65535).astype(np.uint16)
    sl_u16 = (masks["slope"]   * 65535).astype(np.uint16)
    er_u16 = (masks["erosion"] * 65535).astype(np.uint16)
    fl_u16 = (masks["flow"]    * 65535).astype(np.uint16)
    sh_bool = masks["shore"] > 0.5
    dep_u16 = er_u16.copy()
    mc_biomes = _mc_biome_map(biome_grid)

    col_results = core_col.process_tile_columns_v2(
        tile_height    = h_u16,
        tile_slope     = sl_u16,
        tile_erosion   = er_u16,
        tile_flow      = fl_u16,
        tile_deposits  = dep_u16,
        tile_shore     = sh_bool,
        tile_biomes    = biome_grid,
        tile_mc_biomes = mc_biomes,
        tile_origin_x  = col_off,
        tile_origin_y  = row_off,
        noise_gens     = noise,
        cfg            = cfg,
    )

    surface_y = np.array(
        [[cr.surface_y for cr in row] for row in col_results],
        dtype=np.int16,
    )

    # ---- Step 6a: river carving ----
    _log("carve_rivers")
    pre_carve_y = surface_y.copy()
    surface_y, river_meta, _conn_channel_mask = core_river.carve_rivers(
        surface_y        = surface_y,
        flow_tile        = masks["flow"],
        river_tile       = masks.get("river", np.zeros_like(masks["flow"])),
        cfg              = cfg,
        hydro_order      = masks.get("hydro_order"),
        hydro_width      = masks.get("hydro_width"),
        hydro_depth      = masks.get("hydro_depth"),
        hydro_lake       = masks.get("hydro_lake"),
        hydro_lkdep      = masks.get("hydro_lkdep"),
        hydro_lake_wl    = masks.get("hydro_lake_wl"),
        hydro_centerline = masks.get("hydro_centerline"),
        height_norm      = masks["height"],
        masks_dir        = masks_dir,
        tile_x           = tile_x,
        tile_z           = tile_z,
    )

    # S59: Scrap rivers in SAND_DUNE_DESERT — see run_pipeline.py for rationale.
    _sdd_river = (biome_grid == "SAND_DUNE_DESERT") & (river_meta != 3)
    if _sdd_river.any():
        surface_y[_sdd_river]  = pre_carve_y[_sdd_river]
        river_meta[_sdd_river] = 0

    # ---- Step 6b: eco gradients ----
    _log("compute_eco_gradients")
    from core.eco_gradients import compute_cliff_deg
    cliff_deg = compute_cliff_deg(surface_y)
    SEA_LEVEL = 63
    land_mask = surface_y >= SEA_LEVEL

    try:
        eco_grads = core_eco.compute_eco_gradients(
            surface_y        = surface_y,
            flow_f           = masks["flow"],
            erosion_f        = masks["erosion"],
            cliff_deg        = cliff_deg,
            hydro_order      = masks.get("hydro_order", np.zeros_like(masks["height"])),
            hydro_width      = masks.get("hydro_width", np.zeros_like(masks["height"])),
            hydro_lake       = masks.get("hydro_lake",  np.zeros_like(masks["height"])),
            land_mask        = land_mask,
            cfg              = cfg,
            river_meta       = river_meta,
            tile_x           = tile_x,
            tile_z           = tile_z,
            biome_grid       = biome_grid,
            hydro_floodplain = masks.get("hydro_floodplain"),
            wind_windthrow   = masks.get("wind_windthrow"),
            rock_gap         = masks.get("rock_gap"),
            snow_gap         = masks.get("snow_gap"),
            sand_dunes       = masks.get("sand_dunes"),
            beach            = masks.get("beach"),
            override_tile    = masks.get("override"),
        )
    except Exception as e:
        _log(f"eco_gradients failed: {e}")
        eco_grads = None

    # Step 6c: REMOVED S58 (see run_pipeline.py)
    # Step 6c.5: Soften biome boundaries (S58)
    biome_grid = core_biome.soften_biome_boundaries(
        biome_grid, tile_x * TILE_SIZE, tile_z * TILE_SIZE,
        amplitude_px=40.0, scale=200.0, octaves=2,
    )

    # Step 6c2: Padded biome_grid for cross-tile ecotone (S58 Phase 3b)
    # See run_pipeline.py's Step 6c2 for the full rationale on the two halos.
    _INHERITANCE_PAD_PX = 512  # S58: full-tile context on each side
    _ECOTONE_PAD_PX = 48
    biome_grid_padded = None
    try:
        _padded_masks = core_tiles.read_tile(
            masks_dir=masks_dir, col_off=col_off, row_off=row_off,
            width=TILE_SIZE, height=TILE_SIZE,
            pad_px=_INHERITANCE_PAD_PX,
            mask_subset=("height", "slope", "flow", "erosion", "override"),
        )
        _bg_big = core_biome.assign_biomes(
            height_tile=_padded_masks["height"],
            slope_tile=_padded_masks["slope"],
            flow_tile=_padded_masks["flow"],
            erosion_tile=_padded_masks["erosion"],
            override_tile=_padded_masks["override"],
            noise_fields=noise, cfg=cfg,
        )
        _bg_big = core_biome.soften_biome_boundaries(
            _bg_big,
            tile_x * TILE_SIZE - _INHERITANCE_PAD_PX,
            tile_z * TILE_SIZE - _INHERITANCE_PAD_PX,
            amplitude_px=40.0, scale=200.0, octaves=2,
        )
        _bg_big[_INHERITANCE_PAD_PX:_INHERITANCE_PAD_PX + TILE_SIZE,
                _INHERITANCE_PAD_PX:_INHERITANCE_PAD_PX + TILE_SIZE] = biome_grid
        _lo = _INHERITANCE_PAD_PX - _ECOTONE_PAD_PX
        _hi = _INHERITANCE_PAD_PX + TILE_SIZE + _ECOTONE_PAD_PX
        biome_grid_padded = _bg_big[_lo:_hi, _lo:_hi].copy()
        del _bg_big
    except Exception as _ecotone_pad_exc:  # noqa: BLE001
        _log(f"ecotone_pad WARN: {type(_ecotone_pad_exc).__name__}: {_ecotone_pad_exc}")
        biome_grid_padded = None

    # Step 6d: Meadow clearing field (S57 Phase 3a)
    import core.meadow_clearing_field as core_clearing
    clearing_field = core_clearing.compute_meadow_clearing_field(
        tile_x, tile_z, H=surface_y.shape[0], W=surface_y.shape[1]
    )

    # ---- Step 7: surface decoration ----
    _log("decorate_surface")
    _use_geo = bool(cfg.get("lithology", {}).get("feature_flag_enabled", False))
    _use_sp  = bool(cfg.get("surface_pipeline", {}).get("feature_flag_enabled", False))
    surface_blk, sub_blk, ground_cover = core_dec.decorate_surface(
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
        tile_y       = tile_z,
        eco_grads    = eco_grads,
        cliff_deg    = cliff_deg,
        use_new_geology = _use_geo,
        use_new_surface_pipeline = _use_sp,
        lithology_tile = lithology_tile if _use_sp else None,
        clearing_field = clearing_field,
        biome_grid_padded = biome_grid_padded,
    )

    # ---- Step 8 (optional): schematic placement ----
    placements = []
    if place_schematics and schem_index_path and schem_index_path.exists():
        _log("place_schematics")
        try:
            schem_index = core_place.load_index(schem_index_path)
            placements = core_place.place_schematics(
                surface_y     = surface_y,
                biome_grid    = biome_grid,
                river_meta    = river_meta,
                moisture_tile = masks["flow"],
                noise_fields  = noise,
                cfg           = cfg,
                index         = schem_index,
                tile_x        = tile_x,
                tile_y        = tile_z,
                eco_grads     = eco_grads,
                cliff_deg     = cliff_deg,
                clearing_field = clearing_field,
                surface_blocks = surface_blk,
            )
        except Exception as e:
            _log(f"place_schematics failed (non-fatal): {e}")

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return TileArtifacts(
        tile_x=tile_x,
        tile_z=tile_z,
        masks=masks,
        biome_grid=biome_grid,
        surface_y=surface_y,
        pre_carve_y=pre_carve_y,
        river_meta=river_meta,
        eco_grads=eco_grads,
        cliff_deg=cliff_deg,
        surface_blk=surface_blk,
        sub_blk=sub_blk,
        ground_cover=ground_cover,
        lithology_tile=lithology_tile,
        col_results=col_results,
        placements=placements,
        elapsed_ms=elapsed_ms,
    )
