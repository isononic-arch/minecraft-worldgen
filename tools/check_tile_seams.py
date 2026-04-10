"""
check_tile_seams.py — Tile Boundary Seam Checker
Vandir World Generation Pipeline — /tools/check_tile_seams.py

Detects height and biome discontinuities at tile boundaries before a full 50k render.
Runs pipeline steps 4–6a (mask read → biome → column → river carve) for a grid of tiles,
then compares surface_y and biome along every shared edge.

Usage:
    python tools/check_tile_seams.py \\
        --config  config/thresholds.json \\
        --masks   C:/Users/nicho/minecraft-worldgen/masks/ \\
        --tx0 46 --tx1 50 --tz0 44 --tz1 50 \\
        [--height-threshold 4] \\
        [--report seam_report/] \\
        [--threads 4]

Outputs (written to --report directory):
    seam_report.txt     — human-readable summary, sorted worst-first
    seam_heatmap.png    — 2D grid coloured by worst seam at each tile edge
    seams.json          — machine-readable: list of {axis,tx,tz,max_diff,mean_diff,biome_fraction}

Exit codes:
    0  — no seams above threshold
    1  — seams found above threshold
    2  — fatal error
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

WORLD_SIZE_PX  = 50_000
TILE_SIZE_PX   = 512
TILES_PER_AXIS = WORLD_SIZE_PX // TILE_SIZE_PX   # 97

# Default: flag seams where surface_y differs by more than this many blocks
DEFAULT_HEIGHT_THRESHOLD = 4


# ---------------------------------------------------------------------------
# TILE SURFACE COMPUTATION  (runs in worker process)
# ---------------------------------------------------------------------------

def _compute_tile_surface(args: dict) -> dict:
    """
    Run pipeline steps 4–6a for one tile and return the surface_y array
    and biome_grid.

    Uses process_tile_columns_v2 (with uint16 input) — same path as
    validate_test_tile.py — not generate_columns which requires float [0,1]
    and would give a flat Y=-60 result for every tile.

    Returns:
        {
            "tile_x":    int,
            "tile_z":    int,
            "surface_y": (H, W) int16  — MC Y of surface block per column
            "biome_grid": (H, W) str object array
            "error":     str | None
        }
    """
    import importlib
    import sys as _sys
    from pathlib import Path as _Path

    _project_root = str(_Path(__file__).resolve().parent.parent)
    if _project_root not in _sys.path:
        _sys.path.insert(0, _project_root)

    tile_x     = args["tile_x"]
    tile_z     = args["tile_z"]
    cfg_path   = _Path(args["config_path"])
    masks_dir  = _Path(args["masks_dir"])

    try:
        core_tile_stream  = importlib.import_module("core.tile_streamer")
        core_biome_assign = importlib.import_module("core.biome_assignment")
        core_col_gen      = importlib.import_module("core.column_generator")
        core_river        = importlib.import_module("core.river_carver")
        core_noise        = importlib.import_module("core.noise_fields")
        chunk_writer      = importlib.import_module("core.chunk_writer")

        with open(cfg_path) as f:
            cfg = json.load(f)

        col_off = tile_x * TILE_SIZE_PX
        row_off = tile_z * TILE_SIZE_PX
        w = min(TILE_SIZE_PX, WORLD_SIZE_PX - col_off)
        h = min(TILE_SIZE_PX, WORLD_SIZE_PX - row_off)

        if w <= 0 or h <= 0:
            return {"tile_x": tile_x, "tile_z": tile_z,
                    "surface_y": None, "biome_grid": None, "error": "out of world bounds"}

        noise = core_noise.load_noise_generators(cfg_path)

        masks = core_tile_stream.read_tile(
            masks_dir=masks_dir,
            col_off=col_off,
            row_off=row_off,
            width=w,
            height=h,
        )

        biome_grid = core_biome_assign.assign_biomes(
            height_tile   = masks["height"],
            slope_tile    = masks["slope"],
            flow_tile     = masks["flow"],
            erosion_tile  = masks["erosion"],
            override_tile = masks["override"],
            noise_fields  = noise,
            cfg           = cfg,
        )

        # Un-normalise masks back to uint16 for process_tile_columns_v2
        # (same pattern as validate_test_tile.py — generate_columns accepts
        # float [0,1] but mis-casts to uint16 giving all Y=-60 which is wrong)
        h_u16   = (masks["height"]  * 65535).astype(np.uint16)
        sl_u16  = (masks["slope"]   * 65535).astype(np.uint16)
        er_u16  = (masks["erosion"] * 65535).astype(np.uint16)
        fl_u16  = (masks["flow"]    * 65535).astype(np.uint16)
        sh_bool = masks["shore"] > 0.5

        # Build MC biome ID grid
        BIOME_TO_MC = chunk_writer.BIOME_TO_MC
        mc_biomes = np.empty(biome_grid.shape, dtype=object)
        for b in np.unique(biome_grid):
            mc_biomes[biome_grid == b] = BIOME_TO_MC.get(str(b), "minecraft:plains")

        col_results = core_col_gen.process_tile_columns_v2(
            tile_height   = h_u16,
            tile_slope    = sl_u16,
            tile_erosion  = er_u16,
            tile_flow     = fl_u16,
            tile_deposits = er_u16,   # deposits proxied by erosion
            tile_shore    = sh_bool,
            tile_biomes   = biome_grid,
            tile_mc_biomes= mc_biomes,
            tile_origin_x = col_off,
            tile_origin_y = row_off,
            noise_gens    = noise,
            cfg           = cfg,
        )

        # River carving (mutates col_results in-place)
        core_river.carve_tile(
            tile_columns = col_results,
            tile_flow    = masks["flow"],
            cfg          = cfg,
        )

        # Extract surface_y from ColumnResult grid
        surface_y = np.array(
            [[cr.surface_y for cr in row] for row in col_results],
            dtype=np.int16,
        )

        return {
            "tile_x":     tile_x,
            "tile_z":     tile_z,
            "surface_y":  surface_y,
            "biome_grid": biome_grid,
            "error":      None,
        }

    except Exception as exc:
        return {
            "tile_x":     tile_x,
            "tile_z":     tile_z,
            "surface_y":  None,
            "biome_grid": None,
            "error":      traceback.format_exc(),
        }


# ---------------------------------------------------------------------------
# SEAM ANALYSIS
# ---------------------------------------------------------------------------

def _check_x_seam(tile_a: dict, tile_b: dict, height_threshold: int) -> dict:
    """
    Compare the +X edge of tile_a against the -X edge of tile_b.

    tile_a is at (tx, tz), tile_b is at (tx+1, tz).
    Returns seam descriptor dict.
    """
    sy_a = tile_a["surface_y"][:, -1].astype(int)   # last column of A  (Z, )
    sy_b = tile_b["surface_y"][:, 0].astype(int)    # first column of B (Z, )

    diffs = np.abs(sy_a - sy_b)
    max_diff  = int(diffs.max())
    mean_diff = float(diffs.mean())

    # Biome mismatch fraction
    bio_a = tile_a["biome_grid"][:, -1]
    bio_b = tile_b["biome_grid"][:, 0]
    bio_frac = float(np.mean(bio_a != bio_b))

    return {
        "axis":           "X",
        "tx":             tile_a["tile_x"],
        "tz":             tile_a["tile_z"],
        "max_diff":       max_diff,
        "mean_diff":      round(mean_diff, 2),
        "biome_fraction": round(bio_frac, 3),
        "over_threshold": max_diff > height_threshold,
        "diff_profile":   diffs.tolist(),   # per-Z column diffs for PNG
    }


def _check_z_seam(tile_a: dict, tile_b: dict, height_threshold: int) -> dict:
    """
    Compare the +Z edge of tile_a against the -Z edge of tile_b.

    tile_a is at (tx, tz), tile_b is at (tx, tz+1).
    Returns seam descriptor dict.
    """
    sy_a = tile_a["surface_y"][-1, :].astype(int)   # last row of A  (X, )
    sy_b = tile_b["surface_y"][0,  :].astype(int)   # first row of B (X, )

    diffs = np.abs(sy_a - sy_b)
    max_diff  = int(diffs.max())
    mean_diff = float(diffs.mean())

    bio_a = tile_a["biome_grid"][-1, :]
    bio_b = tile_b["biome_grid"][0,  :]
    bio_frac = float(np.mean(bio_a != bio_b))

    return {
        "axis":           "Z",
        "tx":             tile_a["tile_x"],
        "tz":             tile_a["tile_z"],
        "max_diff":       max_diff,
        "mean_diff":      round(mean_diff, 2),
        "biome_fraction": round(bio_frac, 3),
        "over_threshold": max_diff > height_threshold,
        "diff_profile":   diffs.tolist(),
    }


# ---------------------------------------------------------------------------
# REPORTING
# ---------------------------------------------------------------------------

def _write_report(seams: list[dict], report_dir: Path,
                  height_threshold: int, elapsed: float,
                  tile_errors: list[dict]) -> None:
    """Write seam_report.txt and seams.json to report_dir."""
    report_dir.mkdir(parents=True, exist_ok=True)

    over = [s for s in seams if s["over_threshold"]]
    under = [s for s in seams if not s["over_threshold"]]
    over.sort(key=lambda s: -s["max_diff"])

    lines = [
        "=" * 72,
        "TILE SEAM CHECKER — VANDIR",
        f"Height threshold : {height_threshold} blocks",
        f"Seams checked    : {len(seams)}",
        f"ABOVE threshold  : {len(over)}",
        f"below threshold  : {len(under)}",
        f"Tile errors      : {len(tile_errors)}",
        f"Elapsed          : {elapsed:.1f}s",
        "=" * 72,
        "",
    ]

    if tile_errors:
        lines += ["── TILE ERRORS ─────────────────────────────────────────────────"]
        for e in tile_errors:
            lines.append(f"  tile ({e['tile_x']},{e['tile_z']}): {e['error'].splitlines()[-1]}")
        lines.append("")

    if over:
        lines += ["── SEAMS ABOVE THRESHOLD (worst first) ─────────────────────────"]
        for s in over:
            lines.append(
                f"  {s['axis']}-seam at tile ({s['tx']:3d},{s['tz']:3d})  "
                f"max={s['max_diff']:4d}  mean={s['mean_diff']:6.2f}  "
                f"biome_mismatch={s['biome_fraction']:.1%}"
            )
        lines.append("")
    else:
        lines.append("No seams above threshold. All tile boundaries are smooth.")
        lines.append("")

    if under:
        lines += ["── Seams below threshold ─────────────────────────────────────"]
        for s in under[:20]:  # show first 20 only
            lines.append(
                f"  {s['axis']}-seam at tile ({s['tx']:3d},{s['tz']:3d})  "
                f"max={s['max_diff']:4d}  mean={s['mean_diff']:6.2f}"
            )
        if len(under) > 20:
            lines.append(f"  ... and {len(under)-20} more")
        lines.append("")

    report_path = report_dir / "seam_report.txt"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[seams] Report written: {report_path}")

    # JSON
    json_path = report_dir / "seams.json"
    json_path.write_text(
        json.dumps({
            "threshold": height_threshold,
            "elapsed_s": round(elapsed, 1),
            "tile_errors": tile_errors,
            "seams": [{k: v for k, v in s.items() if k != "diff_profile"} for s in seams],
        }, indent=2),
        encoding="utf-8",
    )
    print(f"[seams] JSON written:   {json_path}")


def _write_heatmap(seams: list[dict], report_dir: Path,
                   tx_range: range, tz_range: range,
                   height_threshold: int) -> None:
    """Write a PNG heatmap of max seam diff, one pixel per tile edge."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[seams] Pillow not available — skipping heatmap PNG")
        return

    tx0, tx1 = tx_range.start, tx_range.stop
    tz0, tz1 = tz_range.start, tz_range.stop
    nx = tx1 - tx0   # number of tiles in X
    nz = tz1 - tz0   # number of tiles in Z

    # Grid: one cell per tile, colour = worst seam (X or Z) on its +X or +Z boundary
    # Build max_diff lookup keyed by (axis, tx, tz)
    lookup: dict[tuple, int] = {}
    for s in seams:
        key = (s["axis"], s["tx"], s["tz"])
        lookup[key] = max(lookup.get(key, 0), s["max_diff"])

    CELL = 16   # pixels per tile cell
    W = nx * CELL
    H = nz * CELL
    img = Image.new("RGB", (W, H), (30, 30, 30))
    draw = ImageDraw.Draw(img)

    def _diff_colour(diff: int) -> tuple[int, int, int]:
        if diff == 0:
            return (20, 80, 20)      # green — perfect
        elif diff <= height_threshold:
            t = diff / height_threshold
            # yellow-green gradient
            r = int(180 * t)
            g = int(200 - 80 * t)
            return (r, g, 0)
        else:
            t = min(1.0, (diff - height_threshold) / (height_threshold * 4))
            r = int(200 + 55 * t)
            g = int(60  - 60 * t)
            return (r, g, 0)         # orange → red

    for iz in range(nz):
        for ix in range(nx):
            tx = tx0 + ix
            tz = tz0 + iz
            # Use worst of X and Z seam for this tile
            dx = lookup.get(("X", tx, tz), 0)
            dz = lookup.get(("Z", tx, tz), 0)
            worst = max(dx, dz)
            colour = _diff_colour(worst)
            x0c = ix * CELL
            z0c = iz * CELL
            draw.rectangle([x0c, z0c, x0c + CELL - 1, z0c + CELL - 1], fill=colour)

            # Draw tile coords if cell big enough
            if CELL >= 16:
                draw.text((x0c + 1, z0c + 1), f"{tx},{tz}", fill=(180, 180, 180))

    # Legend
    legend_x = W + 8 if W + 120 < 2048 else 0
    img = img.crop((0, 0, W, H))   # keep compact

    out_path = report_dir / "seam_heatmap.png"
    img.save(out_path)
    print(f"[seams] Heatmap written: {out_path}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Check surface_y seams at tile boundaries")
    ap.add_argument("--config",           required=True,  help="Path to thresholds.json")
    ap.add_argument("--masks",            required=True,  help="Path to masks directory")
    ap.add_argument("--tx0",              type=int, required=True, help="Min tile X (inclusive)")
    ap.add_argument("--tx1",              type=int, required=True, help="Max tile X (inclusive)")
    ap.add_argument("--tz0",              type=int, required=True, help="Min tile Z (inclusive)")
    ap.add_argument("--tz1",              type=int, required=True, help="Max tile Z (inclusive)")
    ap.add_argument("--height-threshold", type=int, default=DEFAULT_HEIGHT_THRESHOLD,
                    help=f"Max allowed surface_y diff at boundary (default {DEFAULT_HEIGHT_THRESHOLD})")
    ap.add_argument("--report",           default="seam_report/", help="Output directory")
    ap.add_argument("--threads",          type=int, default=4)
    args = ap.parse_args()

    cfg_path  = Path(args.config).resolve()
    masks_dir = Path(args.masks).resolve()
    report_dir = Path(args.report).resolve()
    threshold  = args.height_threshold

    tx0, tx1 = args.tx0, args.tx1
    tz0, tz1 = args.tz0, args.tz1

    if tx0 > tx1 or tz0 > tz1:
        print("[seams] ERROR: tx0/tz0 must be <= tx1/tz1", file=sys.stderr)
        return 2

    # Build list of tiles to process (union of both sides of every boundary)
    tiles_needed: set[tuple[int, int]] = set()
    for tx in range(tx0, tx1 + 1):
        for tz in range(tz0, tz1 + 1):
            tiles_needed.add((tx, tz))
    # Also need tx+1 for X-seams and tz+1 for Z-seams
    for tx in range(tx0, tx1):
        for tz in range(tz0, tz1 + 1):
            tiles_needed.add((tx + 1, tz))
    for tx in range(tx0, tx1 + 1):
        for tz in range(tz0, tz1):
            tiles_needed.add((tx, tz + 1))

    tile_list = sorted(tiles_needed)
    n_tiles = len(tile_list)
    print(f"[seams] Processing {n_tiles} tiles with {args.threads} threads …")

    t0 = time.perf_counter()

    worker_args = [
        {
            "tile_x":      tx,
            "tile_z":      tz,
            "config_path": str(cfg_path),
            "masks_dir":   str(masks_dir),
        }
        for tx, tz in tile_list
    ]

    # Run in parallel
    results: dict[tuple[int, int], dict] = {}
    tile_errors: list[dict] = []
    done = 0

    with ProcessPoolExecutor(max_workers=args.threads) as ex:
        futures = {ex.submit(_compute_tile_surface, a): a for a in worker_args}
        for fut in as_completed(futures):
            result = fut.result()
            tx, tz = result["tile_x"], result["tile_z"]
            done += 1
            if result["error"]:
                print(f"[seams] ERROR tile ({tx},{tz}): {result['error'].splitlines()[-1]}")
                tile_errors.append({"tile_x": tx, "tile_z": tz, "error": result["error"]})
            else:
                results[(tx, tz)] = result
                print(f"[seams] [{done}/{n_tiles}] tile ({tx:3d},{tz:3d}) done")

    # Analyse seams
    seams: list[dict] = []

    # X-direction seams: between (tx, tz) and (tx+1, tz)
    for tx in range(tx0, tx1):
        for tz in range(tz0, tz1 + 1):
            a = results.get((tx,     tz))
            b = results.get((tx + 1, tz))
            if a is None or b is None:
                continue
            seams.append(_check_x_seam(a, b, threshold))

    # Z-direction seams: between (tx, tz) and (tx, tz+1)
    for tx in range(tx0, tx1 + 1):
        for tz in range(tz0, tz1):
            a = results.get((tx, tz))
            b = results.get((tx, tz + 1))
            if a is None or b is None:
                continue
            seams.append(_check_z_seam(a, b, threshold))

    elapsed = time.perf_counter() - t0

    n_over = sum(1 for s in seams if s["over_threshold"])
    print(f"[seams] {len(seams)} seams checked — {n_over} above threshold={threshold} blocks")

    tx_range = range(tx0, tx1 + 1)
    tz_range = range(tz0, tz1 + 1)
    _write_report(seams, report_dir, threshold, elapsed, tile_errors)
    _write_heatmap(seams, report_dir, tx_range, tz_range, threshold)

    return 1 if n_over > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
