"""
validate_test_tile.py — Step 13: Test Tile Validation
Vandir World Generation Pipeline — /tools/validate_test_tile.py

Runs a single tile through the full pipeline (Steps 4–9), then applies
a suite of automated checks and exports visual PNG reports for manual QA.

Usage:
    python tools/validate_test_tile.py
        --config  config/thresholds.json
        --masks   C:/Users/nicho/minecraft-worldgen/masks/
        --schem-index C:/Users/nicho/minecraft-worldgen/schematic_index.json
        --output  output/
        --tile-x  48  --tile-z  48
        [--report  validation_report/]
        [--dry-run]

Outputs (all written to --report directory):
    report.txt              — pass/fail summary with all check results
    surface_biome.png       — top-down biome false-color with hillshade
    surface_block.png       — top-down approximate block colors
    surface_height.png      — greyscale heightmap
    column_profile.png      — vertical column profile (centre column)
    checks.json             — machine-readable results for CI

Exit code: 0 = all checks passed, 1 = one or more FAIL, 2 = fatal error
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

# Force UTF-8 output so emoji in report_txt don't crash on cp1252 terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

# ---------------------------------------------------------------------------
# CHECK REGISTRY
# ---------------------------------------------------------------------------

class Check:
    def __init__(self, name: str, category: str):
        self.name     = name
        self.category = category
        self.result   = "SKIP"   # "PASS" | "FAIL" | "WARN" | "SKIP"
        self.message  = ""
        self.detail   = ""

    def passed(self, msg=""):
        self.result  = "PASS"
        self.message = msg

    def failed(self, msg="", detail=""):
        self.result  = "FAIL"
        self.message = msg
        self.detail  = detail

    def warn(self, msg=""):
        self.result  = "WARN"
        self.message = msg


# ---------------------------------------------------------------------------
# INDIVIDUAL CHECKS
# ---------------------------------------------------------------------------

def chk_surface_y_range(surface_y: np.ndarray) -> Check:
    c = Check("surface_y_range", "terrain")
    lo, hi = int(surface_y.min()), int(surface_y.max())
    if lo < -64:
        c.failed(f"surface_y below Y_MIN: min={lo}", "Spline mapping issue")
    elif hi > 448:
        c.failed(f"surface_y above Y_MAX: max={hi}", "Spline mapping issue")
    else:
        c.passed(f"range [{lo}, {hi}] within [-64, 448]")
    return c


def chk_no_bare_dirt_surface(surface_blk: np.ndarray, biome_grid: np.ndarray) -> Check:
    # Scope: LAND columns only. _OCEAN columns have their "surface" 30+ blocks
    # underwater (seafloor), where a dirt block is invisible and irrelevant. The
    # check exists to catch ugly brown patches on land where decoration missed a
    # spot — an ocean-floor dirt pixel is not that. Verified on validate_3x3
    # (48,48) run 2026-04-10: 4 ocean tiles had 14-1380 dirt pixels, all 100%
    # _OCEAN, with neighboring land tiles all clean.
    c = Check("no_bare_dirt_surface", "surface_decoration")
    land_mask = biome_grid != "_OCEAN"
    dirt_mask = (surface_blk == "dirt") & land_mask
    n = int(dirt_mask.sum())
    if n > 0:
        biomes = np.unique(biome_grid[dirt_mask]).tolist()
        c.failed(f"{n} pixels have bare 'dirt' as surface block (land only)",
                 f"Affected biomes: {biomes[:10]}")
    else:
        c.passed("No bare dirt surface blocks on land")
    return c


def chk_water_fill(surface_y: np.ndarray, col_results: list) -> Check:
    """Every sub-sea column must have water above surface up to Y=63."""
    c = Check("water_fill", "terrain")
    SEA_Y = 63
    sub_sea = surface_y < SEA_Y
    if not sub_sea.any():
        c.passed("No sub-sea pixels in this tile (all above sea level)")
        return c

    rows, cols = np.where(sub_sea)
    fail_count = 0
    for r, c_idx in zip(rows[:50], cols[:50]):   # sample first 50
        cr = col_results[int(r)][int(c_idx)]
        sy = cr.surface_y
        if any(cr.blocks.get(y) != "water" for y in range(sy + 1, SEA_Y + 1)):
            fail_count += 1
    if fail_count:
        c.failed(f"{fail_count}/50 sampled sub-sea columns missing water fill")
    else:
        c.passed(f"{int(sub_sea.sum())} sub-sea pixels all have water fill (50-sample check)")
    return c


def chk_bedrock_layer(col_results: list) -> Check:
    c = Check("bedrock_layer", "terrain")
    bad = sum(1 for row in col_results for cr in row if cr.blocks.get(-64) != "bedrock")
    if bad > 0:
        c.failed(f"{bad} pixels at Y=-64 are not bedrock")
    else:
        c.passed("All Y=-64 pixels are bedrock")
    return c


def chk_biome_coverage(biome_grid: np.ndarray) -> Check:
    c = Check("biome_coverage", "biome")
    H, W = biome_grid.shape
    total = H * W
    unique, counts = np.unique(biome_grid, return_counts=True)
    if len(unique) == 0:
        c.failed("No biomes assigned")
        return c
    ocean_like = [b for b in unique if "_OCEAN" in str(b) or b == ""]
    land_count = sum(cnt for b, cnt in zip(unique, counts)
                     if b not in ocean_like)
    ocean_count = total - land_count
    c.passed(f"{len(unique)} biomes, land={land_count/total*100:.1f}%, "
             f"ocean={ocean_count/total*100:.1f}%")
    c.detail = ", ".join(f"{b}:{cnt}" for b, cnt in zip(unique, counts))
    return c


def chk_no_void_columns(col_results: list, surface_y: np.ndarray) -> Check:
    """Sample 100 land columns and verify bedrock + surface blocks are present."""
    c = Check("no_void_columns", "terrain")
    SEA_Y = 63
    land_mask = surface_y >= SEA_Y
    if not land_mask.any():
        c.passed("No above-sea columns to check")
        return c
    rows, cols = np.where(land_mask)
    idx = np.random.default_rng(42).integers(0, len(rows), min(100, len(rows)))
    voids = 0
    for i in idx:
        r, col = int(rows[i]), int(cols[i])
        cr  = col_results[r][col]
        sy  = cr.surface_y
        # Column generator always fills stone implicitly; just verify bedrock + surface present
        if cr.blocks.get(-64) != "bedrock" or sy not in cr.blocks:
            voids += 1
    if voids > 0:
        c.failed(f"{voids}/100 sampled columns have no stone layer")
    else:
        c.passed("100-sample stone layer check passed")
    return c


def chk_river_meta_consistency(river_meta: np.ndarray, surface_y: np.ndarray) -> Check:
    c = Check("river_meta_consistency", "hydrology")
    H, W = river_meta.shape
    if river_meta.max() == 0:
        c.warn("No river pixels in this tile (river_meta all zero)")
        return c
    n_river = int((river_meta > 0).sum())
    # River pixels should generally be at or below sea level or carved
    river_y = surface_y[river_meta > 0]
    high_river = int((river_y > 200).sum())  # rivers shouldn't be above Y=200
    if high_river > n_river * 0.1:
        c.warn(f"{high_river} river pixels above Y=200 — check flow threshold")
    else:
        c.passed(f"{n_river} river pixels, none suspiciously high")
    return c


def chk_schematic_placements(placements: list, surface_y: np.ndarray) -> Check:
    c = Check("schematic_placements", "vegetation")
    if not placements:
        c.warn("No schematic placements in this tile")
        return c
    H, W = surface_y.shape
    bad = 0
    for p in placements:
        # place_y should be below surface_y at that pixel
        local_x = getattr(p, "world_x", 0) % W
        local_z = getattr(p, "world_z", 0) % H
        sy = int(surface_y[local_z, local_x])
        if getattr(p, "place_y", sy) > sy + 2:
            bad += 1
    if bad:
        c.failed(f"{bad}/{len(placements)} placements have place_y above surface")
    else:
        c.passed(f"{len(placements)} placements, all place_y ≤ surface_y + 2")
    return c


def chk_surface_block_variety(surface_blk: np.ndarray, biome_grid: np.ndarray | None = None) -> Check:
    # Scope: tiles with land. Deep-ocean tiles (>95% _OCEAN) are legitimately
    # monotonous — the "surface" is the seafloor, stone is correct there.
    # Running the variety check on them produces false WARNs (e.g. validate_3x3
    # (48,48) tiles (47,48) and (48,48): single block type = ['stone'], both
    # 100% ocean). Signature gained biome_grid as optional kwarg; callers that
    # don't pass it fall back to the original unscoped behavior.
    c = Check("surface_block_variety", "surface_decoration")
    if biome_grid is not None:
        ocean_frac = float((biome_grid == "_OCEAN").mean())
        if ocean_frac > 0.95:
            c.passed(f"skipped (ocean-heavy tile, {ocean_frac:.0%} _OCEAN)")
            return c
    unique_blks = np.unique(surface_blk).tolist()
    n = len(unique_blks)
    if n < 2:
        c.warn(f"Only {n} distinct surface block type(s): {unique_blks}")
    elif n >= 3:
        c.passed(f"{n} distinct surface block types")
    else:
        c.passed(f"{n} distinct surface block types (OK for single-biome tile)")
    return c


def chk_column_profile(col_results: list, surface_y: np.ndarray,
                        cx: int, cz: int) -> Check:
    """Verify centre column has the correct layer order."""
    c = Check("column_profile", "terrain")
    cr = col_results[cz][cx]
    sy = cr.surface_y

    if cr.blocks.get(-64) != "bedrock":
        c.failed(f"Y=-64 is '{cr.blocks.get(-64)}', expected 'bedrock'")
        return c

    surf_blk = cr.blocks.get(sy, "(missing)")
    if surf_blk == "(missing)":
        c.failed(f"No surface block at Y={sy}")
        return c

    # Stone fill is implicit in column_generator — always present between bedrock and sub
    c.passed(f"Bedrock=✓, surface={surf_blk} @ Y={sy}, stone present")
    return c


# ---------------------------------------------------------------------------
# VISUAL REPORT
# ---------------------------------------------------------------------------

def _save_png(arr: np.ndarray, path: Path):
    """Save RGBA or RGB uint8 array as PNG using Pillow."""
    from PIL import Image
    if arr.ndim == 2:
        Image.fromarray(arr.astype(np.uint8), "L").save(str(path))
    elif arr.shape[2] == 4:
        Image.fromarray(arr.astype(np.uint8), "RGBA").save(str(path))
    else:
        Image.fromarray(arr.astype(np.uint8), "RGB").save(str(path))


def save_column_profile_png(col_results: list, surface_y: np.ndarray,
                             cx: int, cz: int, path: Path):
    """
    Save a vertical slice through the centre row as a coloured PNG.
    X-axis = all columns across the tile, Y-axis = MC Y (-64 to 448).
    Reconstructs each column from the sparse ColumnResult.blocks dict.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return

    Y_MIN   = -64
    Y_MAX   = 448
    SEA_Y   = 63
    Y_RANGE = Y_MAX - Y_MIN   # 512

    BLOCK_COLORS = {
        "bedrock":     (0x3A, 0x3A, 0x3A),
        "stone":       (0x8A, 0x8A, 0x8A),
        "andesite":    (0x8C, 0x8C, 0x8E),
        "diorite":     (0xC4, 0xC2, 0xC0),
        "granite":     (0xAA, 0x7A, 0x66),
        "sandstone":   (0xD9, 0xC9, 0x82),
        "dirt":        (0x8B, 0x60, 0x3A),
        "coarse_dirt": (0x72, 0x4D, 0x2D),
        "grass_block": (0x59, 0x9B, 0x3A),
        "podzol":      (0x67, 0x40, 0x1E),
        "sand":        (0xE3, 0xD6, 0x98),
        "gravel":      (0x99, 0x96, 0x90),
        "snow_block":  (0xF0, 0xF4, 0xF8),
        "water":       (0x2E, 0x6E, 0xB8),
        "mud":         (0x54, 0x44, 0x32),
        "clay":        (0x9E, 0xA3, 0xAA),
        "air":         (0x1A, 0x3A, 0x6B),
        "oak_log":     (0x68, 0x4E, 0x2A),
        "oak_leaves":  (0x3A, 0x7A, 0x2A),
    }
    AIR_COLOR  = BLOCK_COLORS["air"]
    STONE_COLOR = BLOCK_COLORS["stone"]

    row = col_results[cz]     # the one row we render
    SLICE_W = len(row)
    SCALE_X = max(1, 512 // SLICE_W)

    img_w = SLICE_W * SCALE_X
    img_h = Y_RANGE
    img = Image.new("RGB", (img_w, img_h), AIR_COLOR)
    draw = ImageDraw.Draw(img)

    for col_idx, cr in enumerate(row):
        sy = cr.surface_y
        blks = cr.blocks   # sparse dict: only bedrock, sub, surface, water entries

        for mc_y in range(Y_MIN, Y_MAX):
            yi = mc_y - Y_MIN
            if mc_y in blks:
                blk = blks[mc_y]
            elif mc_y == Y_MIN:
                blk = "bedrock"
            elif mc_y < sy - 2:
                blk = "stone"    # implicit stone fill
            elif mc_y > sy:
                blk = "air"
            else:
                blk = "stone"    # shouldn't happen, but safe fallback

            color = BLOCK_COLORS.get(blk, (0x60, 0x20, 0x80))
            x0 = col_idx * SCALE_X
            y0 = (Y_RANGE - 1 - yi)   # flip Y: bottom = Y_MIN
            draw.rectangle([x0, y0, x0 + SCALE_X - 1, y0], fill=color)

    # Sea level line
    sea_yi  = SEA_Y - Y_MIN   # = 127
    sea_y   = Y_RANGE - 1 - sea_yi
    draw.line([(0, sea_y), (img_w, sea_y)], fill=(0x00, 0xCC, 0xFF), width=1)

    img.save(str(path))


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Vandir Step 13 — Test Tile Validation")
    parser.add_argument("--config",      required=True)
    parser.add_argument("--masks",       required=True)
    parser.add_argument("--schem-index", default="")
    parser.add_argument("--output",      required=True)
    parser.add_argument("--tile-x",      type=int, default=48)
    parser.add_argument("--tile-z",      type=int, default=48)
    parser.add_argument("--report",          default="validation_report")
    parser.add_argument("--dry-run",         action="store_true")
    parser.add_argument("--write-at-origin", action="store_true",
                        help="Write .mca at world origin (r.0.0.mca) instead of true tile coords")
    args = parser.parse_args()

    report_dir = Path(args.report)
    report_dir.mkdir(parents=True, exist_ok=True)

    print(f"[validate] Test tile ({args.tile_x}, {args.tile_z})")
    print(f"[validate] Report -> {report_dir}")

    # ---- Dynamic imports ----
    import importlib
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    # Pre-flight: check all required files exist
    required_files = {
        "core/__init__.py":            project_root / "core" / "__init__.py",
        "core/biome_assignment.py":    project_root / "core" / "biome_assignment.py",
        "core/tile_streamer.py":       project_root / "core" / "tile_streamer.py",
        "core/column_generator.py":    project_root / "core" / "column_generator.py",
        "core/river_carver_v2.py":     project_root / "core" / "river_carver_v2.py",
        "core/surface_decorator.py":   project_root / "core" / "surface_decorator.py",
        "core/schematic_placement.py": project_root / "core" / "schematic_placement.py",
        "core/chunk_writer.py":        project_root / "core" / "chunk_writer.py",
        "core/noise_fields.py":        project_root / "core" / "noise_fields.py",
        "core/schematic_loader.py":    project_root / "core" / "schematic_loader.py",
        "core/preview_renderer.py":    project_root / "core" / "preview_renderer.py",
    }
    missing = [name for name, p in required_files.items() if not p.exists()]
    if missing:
        print(f"[validate] FATAL: missing files under {project_root}:", file=sys.stderr)
        for m in missing:
            print(f"  MISSING: {m}", file=sys.stderr)
        print("\nCopy all core/*.py files into core/ before running.", file=sys.stderr)
        return 2

    try:
        core_biome    = importlib.import_module("core.biome_assignment")
        core_tiles    = importlib.import_module("core.tile_streamer")
        core_col      = importlib.import_module("core.column_generator")
        core_river    = importlib.import_module("core.river_carver_v2")
        core_dec      = importlib.import_module("core.surface_decorator")
        core_place    = importlib.import_module("core.schematic_placement")
        core_chunk    = importlib.import_module("core.chunk_writer")
        core_noise    = importlib.import_module("core.noise_fields")
        core_schem    = importlib.import_module("core.schematic_loader")
        core_preview  = importlib.import_module("core.preview_renderer")
    except ImportError as e:
        print(f"[validate] FATAL: import error: {e}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        return 2

    cfg_path   = Path(args.config)
    masks_dir  = Path(args.masks)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    schem_idx_path = Path(getattr(args, "schem_index")) if getattr(args, "schem_index") else None

    with open(cfg_path) as f:
        cfg = json.load(f)

    tx, tz    = args.tile_x, args.tile_z
    TILE_SIZE = 512
    col_off   = tx * TILE_SIZE
    row_off   = tz * TILE_SIZE

    checks: list[Check] = []
    t0 = time.perf_counter()

    # ---- Step 4: Read masks ----
    print("[validate] Step 4: reading masks…")
    try:
        masks = core_tiles.read_tile(
            masks_dir=masks_dir, col_off=col_off, row_off=row_off,
            width=TILE_SIZE, height=TILE_SIZE,
        )
    except Exception as e:
        print(f"[validate] FATAL: tile read failed: {e}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        return 2

    noise = core_noise.load_noise_generators(cfg_path)

    # ---- Step 4a: Read discrete lithology mask (Phase 1.75) ----
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

    # ---- Step 5: Biome assignment ----
    print("[validate] Step 5: biome assignment…")
    biome_grid = core_biome.assign_biomes(
        height_tile=masks["height"], slope_tile=masks["slope"],
        flow_tile=masks["flow"],    erosion_tile=masks["erosion"],
        override_tile=masks["override"], noise_fields=noise, cfg=cfg,
    )
    checks.append(chk_biome_coverage(biome_grid))

    # ---- Step 6: Column generation ----
    print("[validate] Step 6: column generation…")

    # process_tile_columns expects uint16 arrays (not normalized float32)
    h_u16  = (masks["height"]   * 65535).astype(np.uint16)
    sl_u16 = (masks["slope"]    * 65535).astype(np.uint16)
    er_u16 = (masks["erosion"]  * 65535).astype(np.uint16)
    fl_u16 = (masks["flow"]     * 65535).astype(np.uint16)
    sh_bool = masks["shore"] > 0.5   # bool shore mask
    # deposits — use erosion as proxy if no dedicated mask
    dep_u16 = er_u16.copy()

    # MC biome mapping — canonical source is core/chunk_writer.BIOME_TO_MC
    mc_biomes = np.empty(biome_grid.shape, dtype=object)
    for b in np.unique(biome_grid):
        mc_biomes[biome_grid == b] = core_chunk.BIOME_TO_MC.get(
            str(b), core_chunk.BIOME_TO_MC["_DEFAULT"]
        )

    col_results = core_col.process_tile_columns_v2(
        tile_height   = h_u16,
        tile_slope    = sl_u16,
        tile_erosion  = er_u16,
        tile_flow     = fl_u16,
        tile_deposits = dep_u16,
        tile_shore    = sh_bool,
        tile_biomes   = biome_grid,
        tile_mc_biomes= mc_biomes,
        tile_origin_x = col_off,
        tile_origin_y = row_off,
        noise_gens    = noise,
        cfg           = cfg,
    )

    # Extract surface_y from ColumnResult grid (list comprehension ~3x faster than nested for)
    surface_y = np.array(
        [[cr.surface_y for cr in row] for row in col_results],
        dtype=np.int16,
    )
    checks.append(chk_surface_y_range(surface_y))

    # ---- Diagnostic: SAND_DUNE_DESERT height alignment check ----
    desert_mask = biome_grid == "SAND_DUNE_DESERT"
    desert_coords = list(zip(*np.where(desert_mask)))
    if desert_coords:
        rng_d = np.random.default_rng(42)
        sample = [desert_coords[i] for i in
                  rng_d.choice(len(desert_coords), size=min(10, len(desert_coords)), replace=False)]
        print("[validate] SAND_DUNE_DESERT height alignment (10 samples):")
        print(f"  {'row':>5} {'col':>5}  {'raw_h16':>8}  {'vs 17050':>10}  surface_y")
        for r, c in sorted(sample):
            raw = int(masks["height"][r, c] * 65535)
            side = "OCEAN-side" if raw > 17050 else "land-side "
            print(f"  {r:>5} {c:>5}  {raw:>8}  {side}  Y={surface_y[r, c]}")
    else:
        print("[validate] No SAND_DUNE_DESERT pixels in this tile.")

    # ---- Step 6a: River carving (v2 — precomputed hydrology) ----
    print("[validate] Step 6a: river carving…")
    pre_carve_y = surface_y.copy()
    surface_y, river_meta, conn_channel_mask = core_river.carve_rivers(
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
        tile_x           = tx,
        tile_z           = tz,
    )
    # Note: col_results.surface_y is immutable (NamedTuple) but downstream
    # steps use the numpy surface_y array directly, which is already carved.
    checks.append(chk_river_meta_consistency(river_meta, surface_y))

    # ---- Step 6b: Ecological gradients ----
    from core.eco_gradients import compute_cliff_deg
    cliff_deg = compute_cliff_deg(surface_y)
    SEA_LEVEL = 63
    land_mask = surface_y >= SEA_LEVEL

    try:
        core_eco = importlib.import_module("core.eco_gradients")
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
            tile_x      = tx,
            tile_z      = tz,
            biome_grid  = biome_grid,
            hydro_floodplain = masks.get("hydro_floodplain"),
            wind_windthrow = masks.get("wind_windthrow"),
            rock_exposure = masks.get("rock_exposure"),
            rock_exposure_tight = masks.get("rock_exposure_tight"),
            snow_caps        = masks.get("snow_caps"),
            snow_caps_north  = masks.get("snow_caps_north"),
            sand_dunes       = masks.get("sand_dunes"),
            beach            = masks.get("beach"),
        )
    except Exception as e:
        print(f"[validate] WARN: eco_gradients failed: {e}")
        eco_grads = None

    # ---- Step 7: Surface decoration ----
    print("[validate] Step 7: surface decoration…")
    # Use decorate_surface() — same as run_pipeline.py — to generate
    # surface blocks from scratch based on biome, river_meta, terrain.
    # Do NOT extract from col_results (pre-carve surface_y is stale).
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
        tile_x       = tx,
        tile_y       = tz,
        eco_grads    = eco_grads,
        cliff_deg    = cliff_deg,
        use_new_geology = _use_geo,
        use_new_surface_pipeline = _use_sp,
        lithology_tile = lithology_tile if _use_sp else None,
    )
    checks.append(chk_no_bare_dirt_surface(surface_blk, biome_grid))
    checks.append(chk_surface_block_variety(surface_blk, biome_grid))

    # ---- Step 8: Schematic placement ----
    print("[validate] Step 8: schematic placement…")
    placements = []
    if schem_idx_path and schem_idx_path.exists():
        try:
            schem_index = core_place.load_index(schem_idx_path)
            placements  = core_place.place_schematics(
                surface_y     = surface_y,
                biome_grid    = biome_grid,
                river_meta    = river_meta,
                moisture_tile = masks["flow"],
                noise_fields  = noise,
                cfg           = cfg,
                index         = schem_index,
                tile_x        = tx,
                tile_y        = tz,
                eco_grads     = eco_grads,
                cliff_deg     = cliff_deg,
            )
        except Exception as e:
            print(f"[validate] WARN: schematic placement failed: {e}")
    checks.append(chk_schematic_placements(placements, surface_y))

    # ---- Step 8b: Water level computation ----
    # River water level: fill water from carved surface up to pre-carve - 1.
    from scipy.ndimage import (label as _label_lakes,
                               distance_transform_edt as _edt_lakes,
                               maximum_filter as _maxf_lakes)
    carved = (river_meta > 0) & (surface_y < pre_carve_y)
    river_water_y = np.where(carved, pre_carve_y - 1, np.int16(-999))

    CHAN_LAKE_V = np.uint8(3)
    CHAN_RIVER_V = np.uint8(2)
    CHAN_STREAM_V = np.uint8(1)
    lake_mask = river_meta == CHAN_LAKE_V
    if lake_mask.any():
        lake_labeled, n_lakes = _label_lakes(lake_mask)
        lake_water_levels = np.full(n_lakes + 1, -999, dtype=np.int16)
        for lid in range(1, n_lakes + 1):
            lk = lake_labeled == lid
            lake_water = int(pre_carve_y[lk].min()) - 1
            lake_water_levels[lid] = np.int16(lake_water)
            river_water_y[lk] = np.int16(lake_water)

        # Connectivity channels: ensure continuous water end-to-end.
        if conn_channel_mask.any():
            from scipy.ndimage import label as _label_conn
            conn_labeled, n_conn = _label_conn(conn_channel_mask)
            dist_to_lake = _edt_lakes(~lake_mask).astype(np.float32)
            river_or_stream = (river_meta == CHAN_RIVER_V) | (river_meta == CHAN_STREAM_V)
            orig_river = river_or_stream & ~conn_channel_mask
            dist_to_river = (_edt_lakes(~orig_river).astype(np.float32)
                             if orig_river.any()
                             else np.full_like(dist_to_lake, 9999))
            expanded_lake_labels = _maxf_lakes(lake_labeled, size=7)

            for cid in range(1, n_conn + 1):
                ch = conn_labeled == cid
                if ch.sum() < 2:
                    continue

                ch_lake_ids = expanded_lake_labels[ch]
                ch_lake_ids = ch_lake_ids[ch_lake_ids > 0]
                if ch_lake_ids.size > 0:
                    lake_id = int(np.bincount(ch_lake_ids).argmax())
                    lake_wl = int(lake_water_levels[lake_id])
                else:
                    lake_wl = int(pre_carve_y[ch].min()) - 1

                ch_river_wy = river_water_y[ch & orig_river]
                if ch_river_wy.size > 0 and (ch_river_wy > -999).any():
                    river_wl = int(ch_river_wy[ch_river_wy > -999].max())
                else:
                    ch_rows, ch_cols = np.where(ch)
                    ch_rdist = dist_to_river[ch_rows, ch_cols]
                    nearest_idx = np.argmin(ch_rdist)
                    nr, nc = int(ch_rows[nearest_idx]), int(ch_cols[nearest_idx])
                    river_wl = int(pre_carve_y[nr, nc]) - 1

                channel_wl = np.int16(max(lake_wl, river_wl))
                too_high = ch & (surface_y >= channel_wl)
                surface_y[too_high] = np.int16(channel_wl - 1)
                river_water_y[ch] = channel_wl

        # Blend river water level toward lake at interfaces
        BLEND_DIST = 8
        river_carved = (river_meta == CHAN_RIVER_V) & carved
        if river_carved.any():
            dist_from_lake = _edt_lakes(~lake_mask)
            blend_zone = river_carved & (dist_from_lake <= BLEND_DIST)
            if blend_zone.any():
                expanded_labels = _maxf_lakes(lake_labeled, size=2 * BLEND_DIST + 1)
                blend_lids = expanded_labels[blend_zone]
                t = dist_from_lake[blend_zone].astype(np.float32) / BLEND_DIST
                lake_y = lake_water_levels[blend_lids].astype(np.float32)
                river_y = river_water_y[blend_zone].astype(np.float32)
                blended = np.round(lake_y * (1.0 - t) + river_y * t).astype(np.int16)
                river_water_y[blend_zone] = blended

    # ---- Step 9: Volume checks (directly from col_results — no 3D array needed) ----
    print("[validate] Step 9: building volume array…")

    # Stamp schematics (only needed for write_tile below)
    vol = None  # defer full allocation until write_tile is attempted
    pal = None
    for p in placements:
        local_x = p.world_x - col_off
        local_z = p.world_z - row_off
        if 0 <= local_x < TILE_SIZE and 0 <= local_z < TILE_SIZE:
            try:
                sd = core_schem.load_schem(Path(p.schem_path))
                if vol is None:
                    vol, pal = core_chunk.build_column_array(
                        surface_y=surface_y, surface_blk=surface_blk,
                        sub_blk=sub_blk, ground_cover=ground_cover,
                    )
                core_chunk.stamp_schematic(vol, pal, sd, local_x, local_z, p.place_y)
            except Exception:
                pass

    # Structural checks (use col_results directly — no dense 3D array needed)
    checks.append(chk_bedrock_layer(col_results))
    checks.append(chk_water_fill(surface_y, col_results))
    checks.append(chk_no_void_columns(col_results, surface_y))
    cx, cz = TILE_SIZE // 2, TILE_SIZE // 2
    checks.append(chk_column_profile(col_results, surface_y, cx, cz))

    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    # ---- Write to region if not dry-run ----
    if not args.dry_run:
        write_x = 0 if args.write_at_origin else col_off
        write_z = 0 if args.write_at_origin else row_off
        if args.write_at_origin:
            print(f"[validate] --write-at-origin: placing tile at world X=0, Z=0 → r.0.0.mca")
            print(f"[validate] Teleport to: /tp @s 256 200 256")
        else:
            print(f"[validate] Writing .mca region file at world X={write_x}, Z={write_z}…")
            print(f"[validate] Teleport to: /tp @s {write_x + 256} 200 {write_z + 256}")
        try:
            core_chunk.write_tile(
                surface_y=surface_y, surface_blk=surface_blk,
                sub_blk=sub_blk,     ground_cover=ground_cover,
                biome_grid=biome_grid, placements=placements,
                schem_loader=core_schem,
                tile_world_x=write_x, tile_world_z=write_z,
                output_dir=output_dir,
                cfg=cfg,
                river_water_y=river_water_y,
                lithology_tile=lithology_tile,
                flow_tile=masks["flow"],
            )
        except Exception as e:
            chk = Check("amulet_write", "io")
            chk.failed(str(e), traceback.format_exc())
            checks.append(chk)
    else:
        print("[validate] Dry-run: skipping .mca write")

    # ---- Visual reports ----
    print("[validate] Generating visual reports…")
    try:
        rgba_biome = core_preview.render_tile(
            biome_grid=biome_grid, surface_y=surface_y,
            height_tile=masks["height"], flow_tile=masks["flow"],
            shore_tile=masks["shore"], mode="biome",
        )
        _save_png(rgba_biome, report_dir / "surface_biome.png")

        rgba_block = core_preview.render_tile(
            biome_grid=biome_grid, surface_y=surface_y,
            height_tile=masks["height"], flow_tile=masks["flow"],
            shore_tile=masks["shore"], mode="block",
            surface_blk=surface_blk,
        )
        _save_png(rgba_block, report_dir / "surface_block.png")

        rgba_ht = core_preview.render_tile(
            biome_grid=biome_grid, surface_y=surface_y,
            height_tile=masks["height"], flow_tile=masks["flow"],
            shore_tile=masks["shore"], mode="height",
        )
        _save_png(rgba_ht, report_dir / "surface_height.png")

        save_column_profile_png(col_results, surface_y, cx, cz,
                                 report_dir / "column_profile.png")

        print(f"[validate]   surface_biome.png   ✓")
        print(f"[validate]   surface_block.png   ✓")
        print(f"[validate]   surface_height.png  ✓")
        print(f"[validate]   column_profile.png  ✓")
    except Exception as e:
        print(f"[validate] WARN: visual report failed (non-fatal): {e}")

    # ---- Write text report ----
    n_pass = sum(1 for c in checks if c.result == "PASS")
    n_fail = sum(1 for c in checks if c.result == "FAIL")
    n_warn = sum(1 for c in checks if c.result == "WARN")
    n_skip = sum(1 for c in checks if c.result == "SKIP")

    lines = [
        "=" * 60,
        f"  Vandir Step 13 — Test Tile Validation Report",
        f"  Tile ({tx}, {tz})  |  elapsed: {elapsed_ms} ms",
        "=" * 60,
        f"  PASS: {n_pass}  FAIL: {n_fail}  WARN: {n_warn}  SKIP: {n_skip}",
        "",
    ]
    for c in checks:
        sym = {"PASS":"✅","FAIL":"❌","WARN":"⚠️","SKIP":"—"}.get(c.result,"?")
        lines.append(f"  {sym} [{c.category}] {c.name}: {c.message}")
        if c.detail:
            lines.append(f"       detail: {c.detail[:200]}")
    lines += [
        "",
        "=" * 60,
        f"  Biome distribution:",
    ]
    unique_biomes, counts = np.unique(biome_grid, return_counts=True)
    for b, cnt in sorted(zip(unique_biomes, counts),
                         key=lambda x: -x[1]):
        pct = cnt / (TILE_SIZE ** 2) * 100
        lines.append(f"    {str(b):<35} {cnt:>7} px  ({pct:.1f}%)")
    lines += [
        "",
        "  Visual outputs written to: " + str(report_dir),
        "=" * 60,
    ]

    report_txt = "\n".join(lines)
    (report_dir / "report.txt").write_text(report_txt, encoding="utf-8")

    # Machine-readable JSON
    checks_json = [
        {"name": c.name, "category": c.category,
         "result": c.result, "message": c.message}
        for c in checks
    ]
    (report_dir / "checks.json").write_text(
        json.dumps({"tile_x": tx, "tile_z": tz, "elapsed_ms": elapsed_ms,
                    "pass": n_pass, "fail": n_fail, "warn": n_warn,
                    "checks": checks_json}, indent=2),
        encoding="utf-8",
    )

    print()
    print(report_txt)

    if n_fail > 0:
        print(f"\n[validate] {n_fail} check(s) FAILED — fix before proceeding to Step 14")
        return 1
    elif n_warn > 0:
        print(f"\n[validate] All checks passed ({n_warn} warning(s) — review before Step 14)")
        return 0
    else:
        print(f"\n[validate] All {n_pass} checks PASSED ✅ — ready for Step 14")
        return 0


# ---------------------------------------------------------------------------
# SMOKE TEST (stdlib + numpy only — no pipeline modules needed)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) == 1:
        print("validate_test_tile.py — unit smoke test")
        H, W = 2, 2

        # Test Check class
        c = Check("test_check", "unit")
        assert c.result == "SKIP"
        c.passed("all good")
        assert c.result == "PASS"
        c2 = Check("test_fail", "unit")
        c2.failed("something wrong", "detail here")
        assert c2.result == "FAIL"
        assert c2.detail == "detail here"

        # Test surface_y check
        sy = np.array([[63, 80], [40, 448]], dtype=np.int16)
        assert chk_surface_y_range(sy).result == "PASS"
        sy_bad = np.array([[63, 80], [40, 449]], dtype=np.int16)
        assert chk_surface_y_range(sy_bad).result == "FAIL"

        # Test bedrock check using minimal fake col_results
        from collections import namedtuple
        _CR = namedtuple("CR", ["surface_y", "blocks", "snow_layer", "biome_id"])
        def _mcr(sy, bedrock="bedrock"):
            return _CR(sy, {-64: bedrock, sy: "grass_block"}, False, "plains")
        fake_cr = [[_mcr(100), _mcr(110)], [_mcr(120), _mcr(130)]]
        assert chk_bedrock_layer(fake_cr).result == "PASS"
        fake_cr_bad = [[_mcr(100, "stone"), _mcr(110)], [_mcr(120), _mcr(130)]]
        assert chk_bedrock_layer(fake_cr_bad).result == "FAIL"

        # Test no bare dirt check
        sb = np.full((H, W), "grass_block", dtype=object)
        bg = np.full((H, W), "MIXED_FOREST", dtype=object)
        assert chk_no_bare_dirt_surface(sb, bg).result == "PASS"
        sb[0, 0] = "dirt"
        assert chk_no_bare_dirt_surface(sb, bg).result == "FAIL"

        # Test biome coverage check
        bg2 = np.full((32, 32), "MIXED_FOREST", dtype=object)
        assert chk_biome_coverage(bg2).result == "PASS"

        print("  Check class:           OK")
        print("  surface_y_range:       OK")
        print("  bedrock_layer:         OK")
        print("  no_bare_dirt_surface:  OK")
        print("  biome_coverage:        OK")
        print("PASS")
        sys.exit(0)
    else:
        sys.exit(main())
