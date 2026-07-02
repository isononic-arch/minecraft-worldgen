"""
tools/validate_3x3.py — Classic 3×3 pre-MCA validator

Runs 9 adjacent tiles (center ± 1) through the pre-MCA pipeline via
tools/_pipeline_runner.py, reuses the existing single-tile check
registry from tools/validate_test_tile.py, stitches the 9 biome and
surface-block arrays into a 1536×1536 strip, and runs seam checks
across the internal tile boundaries.

Designed to be the fast feedback loop for agentic iteration. No .mca
is written. No schematics are placed. Expected wall time on reference
tiles: ~1.5-3 min per tile × 9 = 15-25 min worst case, 3-4 min in
delta mode (--affects-key) where most edits only touch 1-2 tiles.

Usage:
    # Classic 3×3 around (36, 20), all 9 tiles:
    py tools/validate_3x3.py --config config/thresholds.json \
        --masks masks/ --output output/ \
        --tile-x 36 --tile-z 20 \
        --report validation_report_3x3_36_20

    # Delta mode — only tiles affected by a code change:
    py tools/validate_3x3.py ... --affects-key core/surface_decorator.py

    # Baseline diff — compare against committed baseline:
    py tools/validate_3x3.py ... --baseline tests/baselines/3x3/36_20

    # Escape hatch for chunk_writer work — full serial validate_test_tile:
    py tools/validate_3x3.py ... --full

Exit codes:
    0 = all checks PASS (warnings allowed)
    1 = one or more FAIL, or baseline regression
    2 = fatal error (pipeline crash, missing inputs, import failure)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

# Ensure project root + tools/ are importable
_THIS = Path(__file__).resolve()
_PROJECT_ROOT = _THIS.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
if str(_THIS.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent))

# S101: bit-identical vectorized noise, same patch run_pipeline applies.
from core.fast_simplex import install as _fast_simplex_install  # noqa: E402
_fast_simplex_install()

from _pipeline_runner import run_tile_prelude, TileArtifacts, TILE_SIZE  # noqa: E402

# Reuse existing checks from validate_test_tile. These are all single-tile.
import validate_test_tile as vtt  # noqa: E402


TILE_OFFSETS = [
    (-1, -1), (0, -1), (1, -1),
    (-1,  0), (0,  0), (1,  0),
    (-1,  1), (0,  1), (1,  1),
]


# ---------------------------------------------------------------------------
# SEAM CHECKS (multi-tile — cannot live in validate_test_tile's registry)
# ---------------------------------------------------------------------------

def chk_biome_seam_continuity(stitched_biomes: np.ndarray) -> vtt.Check:
    """
    Count biome transitions exactly on an internal tile boundary line
    and compare to transitions one pixel inward. If a seam is real,
    boundary transitions will vastly outnumber interior transitions.
    """
    c = vtt.Check("biome_seam_continuity", "seam")
    H, W = stitched_biomes.shape
    # internal boundaries at x = TILE_SIZE and x = 2*TILE_SIZE,
    # and y = TILE_SIZE and y = 2*TILE_SIZE
    seam_transitions = 0
    inner_transitions = 0
    for x in (TILE_SIZE, 2 * TILE_SIZE):
        left  = stitched_biomes[:, x - 1]
        right = stitched_biomes[:, x]
        inner = stitched_biomes[:, x + 1] if x + 1 < W else right
        seam_transitions  += int(np.sum(left != right))
        inner_transitions += int(np.sum(right != inner))
    for y in (TILE_SIZE, 2 * TILE_SIZE):
        above = stitched_biomes[y - 1, :]
        below = stitched_biomes[y, :]
        inner = stitched_biomes[y + 1, :] if y + 1 < H else below
        seam_transitions  += int(np.sum(above != below))
        inner_transitions += int(np.sum(below != inner))
    # If seams have >2× the natural transition rate of their neighborhood, flag.
    if inner_transitions == 0:
        if seam_transitions > 0:
            c.warn(f"{seam_transitions} biome transitions on seams, 0 interior (small sample)")
        else:
            c.passed("No biome seam transitions detected")
        return c
    ratio = seam_transitions / max(1, inner_transitions)
    c.detail = f"seam={seam_transitions}, interior={inner_transitions}, ratio={ratio:.2f}"
    if ratio > 2.0:
        c.failed(f"Biome seams {ratio:.2f}× interior transition rate (likely real seam)")
    elif ratio > 1.3:
        c.warn(f"Biome seams {ratio:.2f}× interior — watch")
    else:
        c.passed(f"Biome seam ratio {ratio:.2f} (within natural variation)")
    return c


def chk_block_palette_seam_continuity(stitched_blocks: np.ndarray) -> vtt.Check:
    """Same logic as biome seam, but on surface block types."""
    c = vtt.Check("block_palette_seam_continuity", "seam")
    H, W = stitched_blocks.shape
    seam_transitions = 0
    inner_transitions = 0
    for x in (TILE_SIZE, 2 * TILE_SIZE):
        left  = stitched_blocks[:, x - 1]
        right = stitched_blocks[:, x]
        inner = stitched_blocks[:, x + 1] if x + 1 < W else right
        seam_transitions  += int(np.sum(left != right))
        inner_transitions += int(np.sum(right != inner))
    for y in (TILE_SIZE, 2 * TILE_SIZE):
        above = stitched_blocks[y - 1, :]
        below = stitched_blocks[y, :]
        inner = stitched_blocks[y + 1, :] if y + 1 < H else below
        seam_transitions  += int(np.sum(above != below))
        inner_transitions += int(np.sum(below != inner))
    if inner_transitions == 0:
        if seam_transitions > 0:
            c.warn(f"{seam_transitions} block transitions on seams, 0 interior")
        else:
            c.passed("No block palette seam transitions detected")
        return c
    ratio = seam_transitions / max(1, inner_transitions)
    c.detail = f"seam={seam_transitions}, interior={inner_transitions}, ratio={ratio:.2f}"
    if ratio > 2.0:
        c.failed(f"Block palette seams {ratio:.2f}× interior rate")
    elif ratio > 1.3:
        c.warn(f"Block palette seams {ratio:.2f}× interior — watch")
    else:
        c.passed(f"Block palette seam ratio {ratio:.2f}")
    return c


def chk_surface_y_seam_step(stitched_y: np.ndarray, max_step: int = 4) -> vtt.Check:
    """
    Any surface_y discontinuity >max_step blocks across an internal tile
    boundary is a carver or column_generator seam bug (river carver v2
    should have killed these; this check keeps it honest).
    """
    c = vtt.Check("surface_y_seam_step", "seam")
    H, W = stitched_y.shape
    max_observed = 0
    over_budget_px = 0
    for x in (TILE_SIZE, 2 * TILE_SIZE):
        diff = np.abs(stitched_y[:, x - 1].astype(np.int32) -
                      stitched_y[:, x].astype(np.int32))
        max_observed   = max(max_observed, int(diff.max()) if diff.size else 0)
        over_budget_px += int(np.sum(diff > max_step))
    for y in (TILE_SIZE, 2 * TILE_SIZE):
        diff = np.abs(stitched_y[y - 1, :].astype(np.int32) -
                      stitched_y[y, :].astype(np.int32))
        max_observed   = max(max_observed, int(diff.max()) if diff.size else 0)
        over_budget_px += int(np.sum(diff > max_step))
    c.detail = f"max_step={max_observed}, over_budget_px={over_budget_px}"
    if max_observed > max_step * 3:
        c.failed(f"surface_y seam step={max_observed} (budget {max_step})")
    elif max_observed > max_step:
        c.warn(f"surface_y seam step={max_observed} (budget {max_step}, tolerated)")
    else:
        c.passed(f"All seam steps ≤ {max_step} blocks")
    return c


# ---------------------------------------------------------------------------
# AFFECTS MAP (delta-validation)
# ---------------------------------------------------------------------------

def load_affects_map(project_root: Path) -> dict:
    path = project_root / "config" / "validation_affects.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[validate_3x3] WARN: affects map unreadable: {e}")
        return {}


def resolve_affected_tiles(
    affects_map: dict, affects_key: str, center_x: int, center_z: int,
) -> list[tuple[int, int]]:
    """
    Look up affected tiles for a change key (e.g. 'core/surface_decorator.py').
    Returns the list of (tile_x, tile_z) pairs to validate.

    Special values:
        "ALL"      → full 3×3 around center
        "CENTER"   → just the center tile
        list[list] → specific tiles (list of [x, z] pairs), taken verbatim
    """
    default_3x3 = [(center_x + dx, center_z + dz) for dx, dz in TILE_OFFSETS]
    if not affects_key:
        return default_3x3
    entry = affects_map.get(affects_key)
    if entry is None:
        print(f"[validate_3x3] WARN: affects_key '{affects_key}' not in map; running full 3×3")
        return default_3x3
    if entry == "ALL":
        return default_3x3
    if entry == "CENTER":
        return [(center_x, center_z)]
    if isinstance(entry, list):
        out = []
        for pair in entry:
            if isinstance(pair, list) and len(pair) == 2:
                out.append((int(pair[0]), int(pair[1])))
        if out:
            return out
    print(f"[validate_3x3] WARN: affects_key '{affects_key}' malformed; running full 3×3")
    return default_3x3


# ---------------------------------------------------------------------------
# STITCHING
# ---------------------------------------------------------------------------

def stitch_3x3(
    artifacts_by_tile: dict[tuple[int, int], TileArtifacts],
    center_x: int, center_z: int,
    attr: str,
) -> Optional[np.ndarray]:
    """
    Stitch the named attribute of 9 TileArtifacts into a 1536×1536 array.
    Returns None if any of the 9 is missing (caller should skip seam checks).
    """
    expected = [(center_x + dx, center_z + dz) for dx, dz in TILE_OFFSETS]
    if not all(k in artifacts_by_tile for k in expected):
        return None
    # Sample dtype from (0,0) ordering
    sample = getattr(artifacts_by_tile[(center_x - 1, center_z - 1)], attr)
    out = np.empty((3 * TILE_SIZE, 3 * TILE_SIZE), dtype=sample.dtype)
    for dx, dz in TILE_OFFSETS:
        arr = getattr(artifacts_by_tile[(center_x + dx, center_z + dz)], attr)
        row0 = (dz + 1) * TILE_SIZE
        col0 = (dx + 1) * TILE_SIZE
        out[row0:row0 + TILE_SIZE, col0:col0 + TILE_SIZE] = arr
    return out


def _save_grayscale_png(arr: np.ndarray, path: Path) -> None:
    try:
        from PIL import Image
    except ImportError:
        return
    lo, hi = float(arr.min()), float(arr.max())
    rng = max(1.0, hi - lo)
    norm = ((arr.astype(np.float32) - lo) / rng * 255.0).astype(np.uint8)
    Image.fromarray(norm, "L").save(str(path))


def _save_category_png(arr: np.ndarray, path: Path) -> None:
    """Save an object-dtype array as a deterministic pseudocolor PNG."""
    try:
        from PIL import Image
    except ImportError:
        return
    uniq = {v: i for i, v in enumerate(sorted({str(x) for x in np.unique(arr)}))}
    idx = np.vectorize(lambda x: uniq[str(x)])(arr).astype(np.uint16)
    # simple 3-channel hash for visual separation
    r = ((idx * 53) % 251).astype(np.uint8)
    g = ((idx * 97) % 241).astype(np.uint8)
    b = ((idx * 193) % 239).astype(np.uint8)
    rgb = np.stack([r, g, b], axis=-1)
    Image.fromarray(rgb, "RGB").save(str(path))


# ---------------------------------------------------------------------------
# BASELINE DIFF
# ---------------------------------------------------------------------------

def diff_against_baseline(current: dict, baseline_dir: Path) -> list[str]:
    """
    Compare a summary.json against committed baseline. Returns a list of
    regression strings. Empty list = no regressions.
    """
    regressions: list[str] = []
    baseline_path = baseline_dir / "summary.json"
    if not baseline_path.exists():
        return [f"BASELINE-MISSING: {baseline_path}"]
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"BASELINE-UNREADABLE: {e}"]
    # Per-tile checks
    base_tiles = {(t["tile_x"], t["tile_z"]): t for t in baseline.get("tiles", [])}
    curr_tiles = {(t["tile_x"], t["tile_z"]): t for t in current.get("tiles", [])}
    for key, curr in curr_tiles.items():
        base = base_tiles.get(key)
        if not base:
            continue  # a newly-added tile is not a regression
        base_checks = {c["name"]: c["result"] for c in base.get("checks", [])}
        for c in curr.get("checks", []):
            prev = base_checks.get(c["name"], "PASS")
            if prev == "PASS" and c["result"] == "FAIL":
                regressions.append(f"REGRESSION {key} {c['name']}: PASS→FAIL ({c['message']})")
    # Seam checks
    base_seams = {c["name"]: c["result"] for c in baseline.get("seam_checks", [])}
    for c in current.get("seam_checks", []):
        prev = base_seams.get(c["name"], "PASS")
        if prev == "PASS" and c["result"] == "FAIL":
            regressions.append(f"REGRESSION seam {c['name']}: PASS→FAIL ({c['message']})")
    return regressions


# ---------------------------------------------------------------------------
# PER-TILE CHECKS (reuse single-tile registry, skipping schematics + water)
# ---------------------------------------------------------------------------

def run_single_tile_checks(art: TileArtifacts) -> list[vtt.Check]:
    """
    Run the existing single-tile checks from validate_test_tile on a
    TileArtifacts. We skip the .mca-dependent ones (there are none) and
    skip schematic_placements unless placements were actually computed.
    """
    checks: list[vtt.Check] = []
    checks.append(vtt.chk_biome_coverage(art.biome_grid))
    checks.append(vtt.chk_surface_y_range(art.surface_y))
    checks.append(vtt.chk_river_meta_consistency(art.river_meta, art.surface_y))
    checks.append(vtt.chk_no_bare_dirt_surface(art.surface_blk, art.biome_grid))
    checks.append(vtt.chk_surface_block_variety(art.surface_blk, art.biome_grid))
    checks.append(vtt.chk_bedrock_layer(art.col_results))
    checks.append(vtt.chk_water_fill(art.surface_y, art.col_results))
    checks.append(vtt.chk_no_void_columns(art.col_results, art.surface_y))
    cx, cz = TILE_SIZE // 2, TILE_SIZE // 2
    checks.append(vtt.chk_column_profile(art.col_results, art.surface_y, cx, cz))
    if art.placements:
        checks.append(vtt.chk_schematic_placements(art.placements, art.surface_y))
    return checks


def _check_to_dict(c: vtt.Check) -> dict:
    return {
        "name": c.name,
        "category": c.category,
        "result": c.result,
        "message": c.message,
        "detail": c.detail,
    }


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Vandir 3×3 pre-MCA validator")
    p.add_argument("--config", required=True)
    p.add_argument("--masks",  required=True)
    p.add_argument("--output", required=True,
                   help="Pipeline output dir (used by --full escape hatch only)")
    p.add_argument("--tile-x", type=int, required=True)
    p.add_argument("--tile-z", type=int, required=True)
    p.add_argument("--report", default=None,
                   help="Report output dir (default: validation_report_3x3_{x}_{z})")
    p.add_argument("--affects-key", default="",
                   help="Delta mode: look up tiles from config/validation_affects.json")
    p.add_argument("--baseline", default="",
                   help="Path to baseline dir (e.g. tests/baselines/3x3/36_20)")
    p.add_argument("--full", action="store_true",
                   help="Escape hatch: run serial validate_test_tile.py with --full (writes .mca)")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    cx, cz = args.tile_x, args.tile_z
    report_dir = Path(args.report) if args.report else Path(f"validation_report_3x3_{cx}_{cz}")
    report_dir.mkdir(parents=True, exist_ok=True)

    # ---- Escape hatch ----
    if args.full:
        return _run_full_serial(args, report_dir)

    # ---- Resolve tile set ----
    affects_map = load_affects_map(_PROJECT_ROOT)
    tiles = resolve_affected_tiles(affects_map, args.affects_key, cx, cz)
    print(f"[validate_3x3] Center ({cx}, {cz}) | running {len(tiles)} tile(s): {tiles}")

    # ---- Load config ----
    cfg_path = Path(args.config)
    with open(cfg_path) as f:
        cfg = json.load(f)
    masks_dir = Path(args.masks)

    # ---- Run tiles sequentially ----
    artifacts: dict[tuple[int, int], TileArtifacts] = {}
    per_tile_results = []
    t_start = time.perf_counter()
    for (tx, tz) in tiles:
        print(f"[validate_3x3] → tile ({tx}, {tz})")
        try:
            art = run_tile_prelude(
                tile_x=tx, tile_z=tz, cfg=cfg,
                masks_dir=masks_dir, cfg_path=cfg_path,
                place_schematics=False, verbose=args.verbose,
            )
        except Exception as e:
            print(f"[validate_3x3] FAIL: tile ({tx}, {tz}) pipeline crash: {e}")
            print(traceback.format_exc())
            per_tile_results.append({
                "tile_x": tx, "tile_z": tz, "elapsed_ms": 0,
                "checks": [{"name": "pipeline_run", "category": "fatal",
                            "result": "FAIL", "message": str(e), "detail": ""}],
            })
            continue
        checks = run_single_tile_checks(art)
        per_tile_results.append({
            "tile_x": tx, "tile_z": tz, "elapsed_ms": art.elapsed_ms,
            "checks": [_check_to_dict(c) for c in checks],
        })
        # Strip heavy per-tile buffers before retaining for seam stitching.
        # Only biome_grid / surface_blk / surface_y are needed downstream.
        # col_results is the single biggest allocation (262k ColumnResult
        # tuples w/ per-column block dicts) — dropping it saves hundreds of
        # MB per tile, which matters a lot on an 8 GB box running 9 tiles.
        art.col_results = []
        art.masks = {}
        art.pre_carve_y = None  # type: ignore[assignment]
        art.river_meta = None   # type: ignore[assignment]
        art.eco_grads = None
        art.cliff_deg = None    # type: ignore[assignment]
        art.sub_blk = None      # type: ignore[assignment]
        art.ground_cover = None # type: ignore[assignment]
        art.placements = []
        artifacts[(tx, tz)] = art
        import gc
        gc.collect()

    # ---- Seam checks (only if full 3×3 collected) ----
    seam_checks: list[vtt.Check] = []
    stitched_biomes = stitch_3x3(artifacts, cx, cz, "biome_grid")
    stitched_blocks = stitch_3x3(artifacts, cx, cz, "surface_blk")
    stitched_y      = stitch_3x3(artifacts, cx, cz, "surface_y")
    if stitched_biomes is not None:
        seam_checks.append(chk_biome_seam_continuity(stitched_biomes))
        _save_category_png(stitched_biomes, report_dir / "stitched_biomes.png")
    if stitched_blocks is not None:
        seam_checks.append(chk_block_palette_seam_continuity(stitched_blocks))
        _save_category_png(stitched_blocks, report_dir / "stitched_blocks.png")
    if stitched_y is not None:
        seam_checks.append(chk_surface_y_seam_step(stitched_y))
        _save_grayscale_png(stitched_y, report_dir / "stitched_surface_y.png")

    # ---- Summary ----
    elapsed_s = round(time.perf_counter() - t_start, 1)
    summary = {
        "center": [cx, cz],
        "tile_count": len(tiles),
        "elapsed_s": elapsed_s,
        "tiles": per_tile_results,
        "seam_checks": [_check_to_dict(c) for c in seam_checks],
    }

    n_fail = sum(1 for t in per_tile_results
                 for c in t["checks"] if c["result"] == "FAIL")
    n_fail += sum(1 for c in seam_checks if c.result == "FAIL")
    n_warn = sum(1 for t in per_tile_results
                 for c in t["checks"] if c["result"] == "WARN")
    n_warn += sum(1 for c in seam_checks if c.result == "WARN")
    n_pass = sum(1 for t in per_tile_results
                 for c in t["checks"] if c["result"] == "PASS")
    n_pass += sum(1 for c in seam_checks if c.result == "PASS")

    summary["pass"] = n_pass
    summary["fail"] = n_fail
    summary["warn"] = n_warn

    (report_dir / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")

    # Human-readable report
    lines = [
        "=" * 64,
        f"  Vandir 3×3 Pre-MCA Validation",
        f"  Center ({cx}, {cz}) | {len(tiles)} tile(s) | {elapsed_s}s",
        "=" * 64,
        f"  PASS: {n_pass}  FAIL: {n_fail}  WARN: {n_warn}",
        "",
    ]
    for t in per_tile_results:
        lines.append(f"  Tile ({t['tile_x']}, {t['tile_z']})  "
                     f"[{t['elapsed_ms']} ms]")
        for c in t["checks"]:
            sym = {"PASS":"✅","FAIL":"❌","WARN":"⚠️","SKIP":"—"}.get(c["result"], "?")
            lines.append(f"    {sym} [{c['category']}] {c['name']}: {c['message']}")
    if seam_checks:
        lines += ["", "  Seam checks (stitched 3×3):"]
        for c in seam_checks:
            sym = {"PASS":"✅","FAIL":"❌","WARN":"⚠️","SKIP":"—"}.get(c.result, "?")
            lines.append(f"    {sym} [{c.category}] {c.name}: {c.message}")
            if c.detail:
                lines.append(f"        detail: {c.detail}")
    lines += ["", "=" * 64, f"  Report dir: {report_dir}", "=" * 64]
    report_txt = "\n".join(lines)
    (report_dir / "report.txt").write_text(report_txt, encoding="utf-8")
    print()
    print(report_txt)

    # ---- Baseline diff ----
    regressions: list[str] = []
    if args.baseline:
        regressions = diff_against_baseline(summary, Path(args.baseline))
        if regressions:
            print("\n[validate_3x3] BASELINE REGRESSIONS:")
            for r in regressions:
                print(f"  {r}")
            (report_dir / "regressions.txt").write_text(
                "\n".join(regressions), encoding="utf-8")
        else:
            print("\n[validate_3x3] No baseline regressions ✅")

    if n_fail > 0 or regressions:
        return 1
    return 0


def _run_full_serial(args, report_dir: Path) -> int:
    """
    Escape hatch: call the existing single-tile validator 9 times serially,
    writing .mca files. Use this when editing chunk_writer.py or debugging
    NBT issues — anything where pre-MCA validation is blind.
    """
    print("[validate_3x3] --full: running serial validate_test_tile.py 9 times")
    cx, cz = args.tile_x, args.tile_z
    any_fail = False
    for dx, dz in TILE_OFFSETS:
        tx, tz = cx + dx, cz + dz
        sub_report = report_dir / f"tile_{tx}_{tz}"
        sub_report.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            str(_PROJECT_ROOT / "tools" / "validate_test_tile.py"),
            "--config", args.config,
            "--masks",  args.masks,
            "--output", args.output,
            "--tile-x", str(tx),
            "--tile-z", str(tz),
            "--report", str(sub_report),
        ]
        print(f"[validate_3x3] → full tile ({tx}, {tz})")
        rc = subprocess.call(cmd)
        if rc != 0:
            any_fail = True
            print(f"[validate_3x3] tile ({tx}, {tz}) rc={rc}")
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
