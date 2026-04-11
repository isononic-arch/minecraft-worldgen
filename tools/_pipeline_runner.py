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
    placements: list = field(default_factory=list)
    elapsed_ms: int = 0


# Biome name → MC biome string (duplicated from validate_test_tile.py to keep
# this module standalone; both should stay in sync — canonical source is here).
BIOME_TO_MC = {
    "COASTAL_HEATH":           "minecraft:windswept_hills",
    "TEMPERATE_RAINFOREST":    "minecraft:old_growth_spruce_taiga",
    "BOREAL_TAIGA":            "minecraft:taiga",
    "SNOWY_BOREAL_TAIGA":      "minecraft:snowy_taiga",
    "ALPINE_MEADOW":           "minecraft:meadow",
    "ARCTIC_TUNDRA":           "minecraft:frozen_peaks",
    "FROZEN_FLATS":            "minecraft:ice_spikes",
    "TEMPERATE_DECIDUOUS":     "minecraft:forest",
    "RAINFOREST_COAST":        "minecraft:old_growth_birch_forest",
    "RIPARIAN_WOODLAND":       "minecraft:dark_forest",
    "DRY_OAK_SAVANNA":         "minecraft:savanna",
    "KARST_BARRENS":           "minecraft:windswept_gravelly_hills",
    "BIRCH_FOREST":            "minecraft:birch_forest",
    "EASTERN_TEMPERATE_COAST": "minecraft:beach",
    "MIXED_FOREST":            "minecraft:forest",
    "CONTINENTAL_STEPPE":      "minecraft:plains",
    "DRY_PINE_BARRENS":        "minecraft:wooded_badlands",
    "SCRUBBY_HEATHLAND":       "minecraft:windswept_hills",
    "LUSH_RAINFOREST_COAST":   "minecraft:jungle",
    "SAND_DUNE_DESERT":        "minecraft:desert",
    "DESERT_STEPPE_TRANSITION":"minecraft:savanna_plateau",
    "SEMI_ARID_SHRUBLAND":     "minecraft:savanna",
    "DRY_WOODLAND_MAQUIS":     "minecraft:sparse_jungle",
    "TIDAL_JUNGLE_FRINGE":     "minecraft:sparse_jungle",
    "MANGROVE_COAST":          "minecraft:mangrove_swamp",
    "FRESHWATER_FEN":          "minecraft:swamp",
    "_OCEAN":                  "minecraft:ocean",
}


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
    mc_biomes = np.empty(biome_grid.shape, dtype=object)
    for b in np.unique(biome_grid):
        mc_biomes[biome_grid == b] = BIOME_TO_MC.get(str(b), "minecraft:plains")
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
    masks = core_tiles.read_tile(
        masks_dir=masks_dir, col_off=col_off, row_off=row_off,
        width=TILE_SIZE, height=TILE_SIZE,
    )
    noise = core_noise.load_noise_generators(cfg_path)

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

    # ---- Step 6b: eco gradients ----
    _log("compute_eco_gradients")
    _gy, _gx = np.gradient(surface_y.astype(np.float32))
    cliff_deg = np.degrees(np.arctan(np.hypot(_gx, _gy))).astype(np.float32)
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
            rock_exposure    = masks.get("rock_exposure"),
        )
    except Exception as e:
        _log(f"eco_gradients failed: {e}")
        eco_grads = None

    # ---- Step 7: surface decoration ----
    _log("decorate_surface")
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
        col_results=col_results,
        placements=placements,
        elapsed_ms=elapsed_ms,
    )
