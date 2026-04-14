#!/usr/bin/env python3
"""Diagnostic: trace WHERE stone-variant blocks appear on gap==0 gentle-slope pixels.

Instruments surface_decorator.decorate_surface to snapshot surface_blocks at
key stages, then reports per-stage stone counts for MIXED_FOREST gap==0 <18° pixels.

Usage:
    python diag_stone_trace.py --tile-x 51 --tile-z 53
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

STONE_VARIANTS = frozenset({
    "stone", "granite", "diorite", "andesite", "tuff",
    "cobblestone", "mossy_cobblestone", "gravel", "deepslate",
    "calcite", "dripstone_block",
})

MAX_CLIFF_DEG = 18.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tile-x", type=int, default=51)
    ap.add_argument("--tile-z", type=int, default=53)
    ap.add_argument("--config", default="config/thresholds.json")
    ap.add_argument("--masks", default="masks/")
    ap.add_argument("--biome", default="MIXED_FOREST")
    args = ap.parse_args()

    TARGET_BIOME = args.biome

    core_tiles = importlib.import_module("core.tile_streamer")
    core_col   = importlib.import_module("core.column_generator")
    core_river = importlib.import_module("core.river_carver_v2")
    core_dec   = importlib.import_module("core.surface_decorator")
    core_noise = importlib.import_module("core.noise_fields")
    core_biome = importlib.import_module("core.biome_assignment")
    core_eco   = importlib.import_module("core.eco_gradients")
    core_chunk = importlib.import_module("core.chunk_writer")

    cfg = json.load(open(args.config))
    tx, tz = args.tile_x, args.tile_z
    TILE_SIZE = 512
    col_off = tx * TILE_SIZE
    row_off = tz * TILE_SIZE

    print(f"[diag] Tile ({tx},{tz}), target biome={TARGET_BIOME}")
    t0 = time.time()

    # ── Load masks ────────────────────────────────────────────────────────
    masks = core_tiles.read_tile(masks_dir=args.masks, col_off=col_off,
                                  row_off=row_off, width=TILE_SIZE, height=TILE_SIZE)
    noise = core_noise.load_noise_generators(args.config)

    # Lithology
    _lith_col = col_off // 8
    _lith_row = row_off // 8
    _lith_w = max(1, TILE_SIZE // 8)
    _lith_h = max(1, TILE_SIZE // 8)
    lithology_tile = core_tiles.read_discrete_tile(
        Path(args.masks) / "lithology.tif", _lith_col, _lith_row,
        width=_lith_w, height=_lith_h,
    )

    # ── Biomes ────────────────────────────────────────────────────────────
    biome_grid = core_biome.assign_biomes(
        height_tile=masks["height"], slope_tile=masks["slope"],
        flow_tile=masks["flow"], erosion_tile=masks["erosion"],
        override_tile=masks["override"], noise_fields=noise, cfg=cfg,
    )
    print(f"  Biomes: {dict(Counter(biome_grid.ravel().tolist()).most_common(6))}")

    # ── Columns ───────────────────────────────────────────────────────────
    h_u16  = (masks["height"]  * 65535).astype(np.uint16)
    sl_u16 = (masks["slope"]   * 65535).astype(np.uint16)
    er_u16 = (masks["erosion"] * 65535).astype(np.uint16)
    fl_u16 = (masks["flow"]    * 65535).astype(np.uint16)
    sh_bool = masks["shore"] > 0.5
    dep_u16 = er_u16.copy()

    mc_biomes = np.empty(biome_grid.shape, dtype=object)
    for b in np.unique(biome_grid):
        mc_biomes[biome_grid == b] = core_chunk.BIOME_TO_MC.get(
            str(b), core_chunk.BIOME_TO_MC["_DEFAULT"])

    col_results = core_col.process_tile_columns_v2(
        tile_height=h_u16, tile_slope=sl_u16, tile_erosion=er_u16,
        tile_flow=fl_u16, tile_deposits=dep_u16, tile_shore=sh_bool,
        tile_biomes=biome_grid, tile_mc_biomes=mc_biomes,
        tile_origin_x=col_off, tile_origin_y=row_off,
        noise_gens=noise, cfg=cfg,
    )
    surface_y = np.array([[cr.surface_y for cr in row] for row in col_results], dtype=np.int16)

    # ── River carving ─────────────────────────────────────────────────────
    surface_y, river_meta, _ = core_river.carve_rivers(
        surface_y=surface_y, flow_tile=masks["flow"],
        river_tile=masks.get("river", np.zeros_like(masks["flow"])),
        cfg=cfg,
        hydro_order=masks.get("hydro_order"),
        hydro_width=masks.get("hydro_width"),
        hydro_depth=masks.get("hydro_depth"),
        hydro_lake=masks.get("hydro_lake"),
        hydro_lkdep=masks.get("hydro_lkdep"),
        hydro_lake_wl=masks.get("hydro_lake_wl"),
        hydro_centerline=masks.get("hydro_centerline"),
        height_norm=masks["height"],
        masks_dir=Path(args.masks), tile_x=tx, tile_z=tz,
    )

    # ── Eco gradients ─────────────────────────────────────────────────────
    cliff_deg = core_eco.compute_cliff_deg(surface_y)
    land_mask = surface_y >= 63
    eco_grads = core_eco.compute_eco_gradients(
        surface_y=surface_y, flow_f=masks["flow"], erosion_f=masks["erosion"],
        cliff_deg=cliff_deg,
        hydro_order=masks.get("hydro_order", np.zeros_like(masks["height"])),
        hydro_width=masks.get("hydro_width", np.zeros_like(masks["height"])),
        hydro_lake=masks.get("hydro_lake", np.zeros_like(masks["height"])),
        land_mask=land_mask, cfg=cfg, river_meta=river_meta,
        tile_x=tx, tile_z=tz, biome_grid=biome_grid,
        hydro_floodplain=masks.get("hydro_floodplain"),
        wind_windthrow=masks.get("wind_windthrow"),
        rock_exposure=masks.get("rock_exposure"),
        rock_exposure_tight=masks.get("rock_exposure_tight"),
        snow_caps=masks.get("snow_caps"),
        snow_caps_north=masks.get("snow_caps_north"),
        sand_dunes=masks.get("sand_dunes"),
        beach=masks.get("beach"),
    )

    H, W = surface_y.shape
    gap = eco_grads.gap_mask

    # ── Build scope ───────────────────────────────────────────────────────
    scope = (gap == 0) & (biome_grid == TARGET_BIOME) & (cliff_deg < MAX_CLIFF_DEG)
    n_scope = int(scope.sum())
    print(f"\n  Scope (gap==0, {TARGET_BIOME}, cliff<{MAX_CLIFF_DEG}°): {n_scope} px")
    if n_scope == 0:
        # Try without biome filter
        scope_any = (gap == 0) & (cliff_deg < MAX_CLIFF_DEG)
        n_any = int(scope_any.sum())
        n_target = int((biome_grid == TARGET_BIOME).sum())
        print(f"  gap==0 & cliff<18: {n_any}, {TARGET_BIOME} total: {n_target}")
        print(f"  gap distribution for {TARGET_BIOME}: {Counter(gap[biome_grid == TARGET_BIOME].tolist())}")
        if n_target == 0:
            print(f"  No {TARGET_BIOME} in this tile. Try --biome TEMPERATE_DECIDUOUS etc.")
            return
        return

    cd_vals = cliff_deg[scope]
    for lo, hi in [(0,2),(2,5),(5,8),(8,12),(12,18)]:
        n = int(((cd_vals >= lo) & (cd_vals < hi)).sum())
        print(f"    cliff_deg [{lo:2d}-{hi:2d}°): {n:5d} px ({100*n/n_scope:.1f}%)")

    # ── Monkey-patch decorate_surface to capture snapshots ────────────────
    snapshots = {}

    orig_noise_layers = core_dec._apply_noise_layers
    def patched_nl(surface_blocks, *a, **kw):
        orig_noise_layers(surface_blocks, *a, **kw)
        snapshots["01_after_noise_layers"] = surface_blocks[scope].copy()
    core_dec._apply_noise_layers = patched_nl

    orig_ecotone = core_dec._apply_ecotone_dither
    def patched_eco(surface_blocks, subsurface_blocks, *a, **kw):
        snapshots["02_before_ecotone"] = surface_blocks[scope].copy()
        orig_ecotone(surface_blocks, subsurface_blocks, *a, **kw)
        snapshots["03_after_ecotone"] = surface_blocks[scope].copy()
    core_dec._apply_ecotone_dither = patched_eco

    # ── Run decoration ────────────────────────────────────────────────────
    _use_geo = bool(cfg.get("lithology", {}).get("feature_flag_enabled", False))
    _use_sp  = bool(cfg.get("surface_pipeline", {}).get("feature_flag_enabled", False))
    print(f"\n  use_new_geology={_use_geo}, use_new_surface_pipeline={_use_sp}")

    surface_blk, sub_blk, ground_cover = core_dec.decorate_surface(
        surface_y=surface_y, biome_grid=biome_grid,
        erosion_tile=masks["erosion"], moisture_tile=masks["flow"],
        height_tile=masks["height"], river_meta=river_meta,
        flow_tile=masks["flow"], noise_fields=noise,
        cfg=cfg, tile_x=tx, tile_y=tz,
        eco_grads=eco_grads, cliff_deg=cliff_deg,
        use_new_geology=_use_geo,
        use_new_surface_pipeline=_use_sp,
        lithology_tile=lithology_tile if _use_sp else None,
    )
    snapshots["04_final"] = surface_blk[scope].copy()

    # Restore patches
    core_dec._apply_noise_layers = orig_noise_layers
    core_dec._apply_ecotone_dither = orig_ecotone

    # ── Report ────────────────────────────────────────────────────────────
    elapsed = time.time() - t0
    print(f"\n  Pipeline complete ({elapsed:.1f}s)")
    print(f"\n  BLOCK ANALYSIS: gap==0 {TARGET_BIOME} cliff<{MAX_CLIFF_DEG}° ({n_scope} px)")
    print(f"  {'='*70}")

    prev_stage = None
    for stage, blocks in sorted(snapshots.items()):
        stone_mask = np.isin(blocks, list(STONE_VARIANTS))
        n_stone = int(stone_mask.sum())
        print(f"\n  ── {stage} ──")
        print(f"     Stone variants: {n_stone}/{n_scope} ({100*n_stone/n_scope:.1f}%)")
        if n_stone > 0:
            c = Counter(blocks[stone_mask].tolist())
            for blk, cnt in c.most_common():
                print(f"       {blk:25s} {cnt:5d} ({100*cnt/n_scope:.1f}%)")
        c_all = Counter(blocks.tolist())
        top5 = c_all.most_common(5)
        print(f"     Top 5: {', '.join(f'{b}:{n}' for b,n in top5)}")

        # Diff from previous stage
        if prev_stage is not None:
            prev_blocks = snapshots[prev_stage]
            new_stone = np.isin(blocks, list(STONE_VARIANTS)) & ~np.isin(prev_blocks, list(STONE_VARIANTS))
            n_new = int(new_stone.sum())
            if n_new > 0:
                c = Counter(blocks[new_stone].tolist())
                print(f"     +INTRODUCED from {prev_stage}: {n_new} stone px — {dict(c)}")
            lost_stone = ~np.isin(blocks, list(STONE_VARIANTS)) & np.isin(prev_blocks, list(STONE_VARIANTS))
            n_lost = int(lost_stone.sum())
            if n_lost > 0:
                print(f"     -REMOVED from {prev_stage}: {n_lost} stone px")
        prev_stage = stage

    # ── All-biome summary ─────────────────────────────────────────────────
    all_gentle_gap0 = (gap == 0) & (cliff_deg < MAX_CLIFF_DEG)
    all_stone = np.isin(surface_blk, list(STONE_VARIANTS))
    n_gentle = int(all_gentle_gap0.sum())
    n_gentle_stone = int((all_gentle_gap0 & all_stone).sum())
    print(f"\n  ALL BIOMES gap==0 cliff<{MAX_CLIFF_DEG}°: {n_gentle_stone}/{n_gentle} stone ({100*n_gentle_stone/n_gentle:.1f}%)")

    for bname in sorted(set(biome_grid[all_gentle_gap0].ravel().tolist())):
        bscope = all_gentle_gap0 & (biome_grid == bname)
        n_b = int(bscope.sum())
        n_bs = int((bscope & all_stone).sum())
        if n_b > 0 and n_bs > 0:
            pct = 100*n_bs/n_b
            c = Counter(surface_blk[bscope & all_stone].tolist())
            top = ", ".join(f"{b}:{n}" for b,n in c.most_common(4))
            print(f"    {str(bname):30s} {n_bs:5d}/{n_b:5d} ({pct:5.1f}%) — {top}")


if __name__ == "__main__":
    main()
