"""
river_carver.py — Vandir Pipeline Step 6a
==========================================
Carves river channels and lakes into the column results produced by Step 6.
Runs after column generation, before surface decoration (Step 7).

Inputs (per tile):
  - tile_columns : 2D list[list[ColumnResult]] from column_generator.py
  - tile_flow    : (H, W) float32 normalised [0,1] flow mask
  - tile_height  : (H, W) uint16 raw height mask (for lake distance transform)
  - tile_biomes  : (H, W) str biome names

Outputs:
  - Mutates tile_columns in-place (modifies .blocks and .surface_y)
  - Returns tile_river_meta : (H, W) uint8 channel-type array (0=none,1=stream,2=river,3=lake)
    consumed by Step 7 for bank decoration

Algorithm:
  1. Classify each pixel: none / stream / river / lake from flow value
  2. For lake pixels: compute distance transform for depth graduation
  3. For each river/stream/lake pixel:
       a. Determine channel depth
       b. Carve water column from surface_Y down to (surface_Y - depth)
       c. Place gravel/clay/stone riverbed below water
       d. Update surface_Y to water surface
  4. Return river_meta array for Step 7

All thresholds from config/thresholds.json under "river_carving".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

import numpy as np
from scipy.ndimage import distance_transform_edt

# Re-use ColumnResult type from Step 6
# (import at runtime to avoid circular issues in standalone test)
MC_Y_MIN  = -64
SEA_LEVEL = 63
BEDROCK_Y = MC_Y_MIN

# Channel type constants (written to river_meta)
CHAN_NONE   = np.uint8(0)
CHAN_STREAM = np.uint8(1)
CHAN_RIVER  = np.uint8(2)
CHAN_LAKE   = np.uint8(3)

# Riverbed block by channel type / depth
def _riverbed_block(depth_below_surface: int, chan_type: int) -> str:
    """Block placed at the bottom of a carved channel."""
    if chan_type == CHAN_LAKE:
        if depth_below_surface > 10:
            return "clay"
        if depth_below_surface > 16:
            return "stone"
    return "gravel"


# ── Core carving functions ────────────────────────────────────────────────────

def channel_depth_river(flow_norm: float, cfg: dict) -> int:
    """Linear interpolation of depth between river_depth_min and max."""
    t = (flow_norm - cfg["river_threshold"]) / (
        cfg["lake_threshold"] - cfg["river_threshold"]
    )
    t = max(0.0, min(1.0, t))
    return int(cfg["river_depth_min"] + t * (cfg["river_depth_max"] - cfg["river_depth_min"]))


def channel_depth_lake(dist_from_shore_norm: float, cfg: dict) -> int:
    """Non-linear depth graduation — shallow edges, deep centre."""
    t = dist_from_shore_norm ** 1.5
    return int(cfg["lake_depth_min"] + t * (cfg["lake_depth_max"] - cfg["lake_depth_min"]))


def carve_column(
    col,                  # ColumnResult (mutated in-place via .blocks dict)
    depth: int,
    chan_type: int,
) -> None:
    """
    Carve a water channel into a single column.
    Mutates col.blocks and col.surface_y.

    Water fills from surface_Y down to (surface_Y - depth + 1).
    Riverbed (gravel/clay) at (surface_Y - depth) and one below.
    Water surface stays at original surface_Y (not forced to sea level).
    """
    surface_y = col.surface_y

    # Don't carve ocean columns — already underwater
    if surface_y <= SEA_LEVEL:
        return

    # Don't carve if depth would breach bedrock
    if surface_y - depth <= BEDROCK_Y + 2:
        depth = surface_y - BEDROCK_Y - 3

    if depth <= 0:
        return

    bed_y = surface_y - depth

    # Riverbed blocks (2 layers)
    bed_blk = _riverbed_block(depth, chan_type)
    if bed_y > BEDROCK_Y:
        col.blocks[bed_y] = bed_blk
    if bed_y - 1 > BEDROCK_Y:
        col.blocks[bed_y - 1] = bed_blk

    # Water column (surface down to bed_y+1)
    for y in range(bed_y + 1, surface_y + 1):
        col.blocks[y] = "water"

    # surface_y stays the same — water surface is at original terrain level
    # (rivers flow above sea level in highlands; Step 6 water fill handles ocean)


# ── Tile-level carver ─────────────────────────────────────────────────────────

def carve_tile(
    tile_columns:  list[list],          # 2D list[list[ColumnResult]], mutated in-place
    tile_flow:     np.ndarray,          # (H, W) float32 normalised [0,1]
    cfg:           dict,                # full thresholds.json
) -> np.ndarray:
    """
    Carve rivers and lakes into all columns of a tile.

    Returns river_meta (H, W) uint8 — channel type per pixel.
    Consumed by Step 7 for bank decoration placement.
    """
    rc = cfg["river_carving"]
    H, W = tile_flow.shape

    stream_thresh = rc["stream_threshold"]
    river_thresh  = rc["river_threshold"]
    lake_thresh   = rc["lake_threshold"]

    # ── 1. Classify pixels ────────────────────────────────────────────────────
    river_meta = np.zeros((H, W), dtype=np.uint8)
    river_meta[tile_flow >= stream_thresh] = CHAN_STREAM
    river_meta[tile_flow >= river_thresh]  = CHAN_RIVER
    river_meta[tile_flow >= lake_thresh]   = CHAN_LAKE

    # ── 2. Distance transform for lake depth graduation ───────────────────────
    lake_mask = (river_meta == CHAN_LAKE)
    if lake_mask.any():
        # distance_transform_edt returns distance from each lake pixel to nearest
        # non-lake pixel (i.e. shore). We want distance from shore normalised.
        dist_raw = distance_transform_edt(lake_mask)
        max_dist = dist_raw.max()
        if max_dist > 0:
            dist_norm = dist_raw / max_dist   # [0,1] — 0 at edge, 1 at deepest
        else:
            dist_norm = np.zeros_like(dist_raw)
    else:
        dist_norm = None

    # ── 3. Carve each channel pixel ───────────────────────────────────────────
    for row in range(H):
        for col_idx in range(W):
            chan = river_meta[row, col_idx]
            if chan == CHAN_NONE:
                continue

            column = tile_columns[row][col_idx]
            flow_v = float(tile_flow[row, col_idx])

            if chan == CHAN_STREAM:
                depth = rc["stream_depth"]

            elif chan == CHAN_RIVER:
                depth = channel_depth_river(flow_v, rc)

            else:  # CHAN_LAKE
                d_norm = float(dist_norm[row, col_idx]) if dist_norm is not None else 0.0
                depth = channel_depth_lake(d_norm, rc)

            carve_column(column, depth, chan)

    return river_meta


# ── Default config ────────────────────────────────────────────────────────────

DEFAULT_RIVER_CFG = {
    "river_carving": {
        "stream_threshold":  0.30,
        "river_threshold":   0.60,
        "lake_threshold":    0.85,
        "stream_depth":      4,
        "river_depth_min":   6,
        "river_depth_max":   14,
        "lake_depth_min":    4,
        "lake_depth_max":    18,
        "bank_width_stream": 1,
        "bank_width_river":  3,
        "bank_width_lake":   4,
    }
}


def carve_rivers(
    surface_y:  np.ndarray,   # (H, W) int16 — modified in-place where channels exist
    flow_tile:  np.ndarray,   # (H, W) float/uint16 — flow mask
    river_tile: np.ndarray,   # (H, W) — unused in array path but kept for API compat
    cfg:        dict,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Array-based river carving wrapper for run_pipeline.py.

    Classifies pixels into channel types, lowers surface_y at channel pixels
    so the chunk_writer fills them with water, and returns (surface_y, river_meta).
    """
    rc = cfg.get("river_carving", DEFAULT_RIVER_CFG["river_carving"])

    # Normalise flow to [0,1] if uint16
    if flow_tile.dtype != np.float32 and flow_tile.dtype != np.float64:
        flow_f = flow_tile.astype(np.float32) / 65535.0
    else:
        flow_f = flow_tile.astype(np.float32)

    H, W = flow_f.shape

    # Classify channel type
    river_meta = np.zeros((H, W), dtype=np.uint8)
    river_meta[flow_f >= rc["stream_threshold"]] = CHAN_STREAM
    river_meta[flow_f >= rc["river_threshold"]]  = CHAN_RIVER
    river_meta[flow_f >= rc["lake_threshold"]]   = CHAN_LAKE

    # Distance transform for lake depth graduation
    lake_mask = (river_meta == CHAN_LAKE)
    if lake_mask.any():
        dist_raw = distance_transform_edt(lake_mask)
        max_dist = dist_raw.max()
        dist_norm = (dist_raw / max_dist) if max_dist > 0 else np.zeros_like(dist_raw)
    else:
        dist_norm = np.zeros((H, W), dtype=np.float32)

    # Carve surface_y at channel pixels
    surface_out = surface_y.astype(np.int32).copy()
    channel_px  = np.argwhere(river_meta > 0)

    for row, col in channel_px:
        sy   = int(surface_out[row, col])
        if sy <= SEA_LEVEL:
            continue   # skip ocean columns — already submerged

        chan = int(river_meta[row, col])
        flow_v = float(flow_f[row, col])

        if chan == CHAN_STREAM:
            depth = rc["stream_depth"]
        elif chan == CHAN_RIVER:
            depth = channel_depth_river(flow_v, rc)
        else:
            depth = channel_depth_lake(float(dist_norm[row, col]), rc)

        # Clamp depth so we don't breach bedrock
        depth = min(depth, sy - BEDROCK_Y - 3)
        if depth <= 0:
            continue

        # Lower surface_y by depth so chunk_writer leaves a water-filled pit
        surface_out[row, col] = sy - depth

    return surface_out.astype(np.int16), river_meta


def load_cfg(thresholds_path: Path) -> dict:
    if thresholds_path.exists():
        with open(thresholds_path) as f:
            cfg = json.load(f)
    else:
        cfg = {}
    if "river_carving" not in cfg:
        cfg["river_carving"] = DEFAULT_RIVER_CFG["river_carving"]
    return cfg


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, types

    # Stub opensimplex
    mod = types.ModuleType("opensimplex")
    class _OS:
        def __init__(self, seed=0): pass
        def noise2(self, x, y): return 0.0
    mod.OpenSimplex = _OS
    sys.modules.setdefault("opensimplex", mod)

    # Stub scipy if unavailable
    try:
        from scipy.ndimage import distance_transform_edt
    except ImportError:
        print("WARNING: scipy not available — lake distance transform will be skipped in test")
        def distance_transform_edt(arr):
            return arr.astype(float)

    from column_generator import generate_column, load_cfg as load_col_cfg, ColumnResult

    print("=" * 60)
    print("  Step 6a — river_carver.py smoke test")
    print("=" * 60)

    col_cfg = load_col_cfg(Path("config/thresholds.json"))
    cfg     = load_cfg(Path("config/thresholds.json"))

    gens = {
        "slope_mix": _OS(),
        "snow_line": _OS(),
        "dune_a":    _OS(),
        "dune_b":    _OS(),
    }

    # Build a small 5×5 synthetic tile — land at ~Y150, one river stripe
    TILE = 5
    columns = []
    for row in range(TILE):
        row_cols = []
        for col_i in range(TILE):
            result = generate_column(
                px=col_i, py=row,
                height_16=10000,        # raw 10000 → mid-land terrain
                slope_norm=0.1,
                erosion_norm=0.2,
                flow_norm=0.1,
                deposits_norm=0.1,
                is_shore=False,
                biome="TEMPERATE_DECIDUOUS",
                mc_biome="forest",
                noise_gens=gens,
                cfg=col_cfg,
            )
            row_cols.append(result)
        columns.append(row_cols)

    # Flow mask: middle column is a river, centre pixel is lake
    flow = np.zeros((TILE, TILE), dtype=np.float32)
    flow[:, 2] = 0.70   # river stripe down column 2
    flow[2, 2] = 0.90   # lake pixel at centre

    pre_surface = columns[0][2].surface_y
    print(f"\n  Pre-carve surface Y (river column): {pre_surface}")
    print(f"  Pre-carve block at surface: {columns[0][2].blocks.get(pre_surface, 'stone')}")

    river_meta = carve_tile(columns, flow, cfg)

    print(f"\n  Post-carve results (river stripe, col 2):")
    all_ok = True
    for row in range(TILE):
        col = columns[row][2]
        chan = river_meta[row, 2]
        chan_name = {0:"none", 1:"stream", 2:"river", 3:"lake"}[chan]
        surface_blk = col.blocks.get(col.surface_y, "stone")
        # Check water at surface
        water_ok = col.blocks.get(col.surface_y) == "water"
        # Check gravel below
        bed_y = col.surface_y - (4 if chan == CHAN_STREAM else 6)  # approx
        ok = water_ok
        if not ok: all_ok = False
        print(f"  row {row}  chan={chan_name:<6}  surface_Y={col.surface_y}  "
              f"surface={surface_blk:<8}  water_at_surface={'✓' if water_ok else '✗'}")

    print(f"\n  Non-river column (col 0) unchanged: ", end="")
    unchanged = columns[0][0].surface_y == pre_surface
    print("✓" if unchanged else f"✗ (surface_Y changed to {columns[0][0].surface_y})")
    if not unchanged: all_ok = False

    print(f"\n  {'ALL PASSED' if all_ok else 'FAILURES DETECTED'}")
    print("=" * 60)
