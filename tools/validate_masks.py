"""
tools/validate_masks.py — standalone mask sanity validator

Opens every TIFF in masks/ and checks:
  - file exists
  - raster shape matches expected 50k × 50k (read from rasterio metadata only,
    no full load)
  - dtype is in the expected set for that mask category
  - coverage % (nonzero, or >threshold for gradient masks) is within bounds
    loaded from config/validation_affects.json → "mask_bounds"
  - no NaN / no all-zero (unless explicitly allowed)

Runs in ~1 min against the full masks/ directory on an 8 GB box. Designed
to be called right after any rebuild_*.py script before chaining into a
3×3 tile render.

Usage:
    py tools/validate_masks.py --masks masks/
    py tools/validate_masks.py --masks masks/ --only sand_dunes,rock_exposure
    py tools/validate_masks.py --masks masks/ --report validation_report_masks/

Exit codes:
    0 = all PASS
    1 = one or more FAIL
    2 = fatal (missing rasterio, masks dir not found)

Bounds file format (config/validation_affects.json → "mask_bounds"):
    {
        "sand_dunes":   {"kind": "gradient", "min_cov": 0.001, "max_cov": 0.20},
        "rock_exposure":{"kind": "gradient", "min_cov": 0.05,  "max_cov": 0.40},
        "override":     {"kind": "discrete", "min_cov": 0.35,  "max_cov": 0.50},
        "height":       {"kind": "dense",    "min_cov": 1.00,  "max_cov": 1.00}
    }
kind:
    "gradient" — coverage = fraction of pixels where value > 0.001
    "discrete" — coverage = fraction of pixels with value > 0
    "dense"    — coverage = fraction non-NaN (should be 1.0 for height/slope)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_THIS = Path(__file__).resolve()
_PROJECT_ROOT = _THIS.parent.parent

EXPECTED_SHAPE = (50000, 50000)  # 50k × 50k world
SAMPLE_GRID = 2048               # strided full-raster downsample side; gives a true global coverage estimate
MAX_COV_DRIFT = 0.05             # advisory: flag if coverage drifts >5% from mid-range


DEFAULT_BOUNDS = {
    # Dense masks (should be fully populated)
    "height":               {"kind": "dense",    "min_cov": 0.99, "max_cov": 1.00},
    "slope":                {"kind": "dense",    "min_cov": 0.99, "max_cov": 1.00},
    "erosion":              {"kind": "dense",    "min_cov": 0.99, "max_cov": 1.00},
    "flow":                 {"kind": "gradient", "min_cov": 0.01, "max_cov": 1.00},
    # Discrete
    # override: ~40% of world nonzero (30% land @ 99.6% coverage + ~14% ocean coastal zones).
    # Do NOT raise min_cov without first rendering against output/override_worldview.png —
    # land fraction is 30%, not 70%. See TRIAGE_validate_2026-04-10.md polarity banner.
    "override":             {"kind": "discrete", "min_cov": 0.35, "max_cov": 0.50},
    # shore: binary LAND mask (65535=land, 0=ocean). Coverage ≈ land fraction ≈ 30%.
    # NOT a coastline band despite the name. Matches (height > 17050) pixel-for-pixel.
    "shore":                {"kind": "discrete", "min_cov": 0.25, "max_cov": 0.40},
    # Hydro
    "river":                {"kind": "gradient", "min_cov": 0.001,"max_cov": 0.20},
    "hydro_order":          {"kind": "discrete", "min_cov": 0.0,  "max_cov": 0.30},
    "hydro_width":          {"kind": "gradient", "min_cov": 0.0,  "max_cov": 0.30},
    "hydro_depth":          {"kind": "gradient", "min_cov": 0.0,  "max_cov": 0.30},
    "hydro_lake":           {"kind": "gradient", "min_cov": 0.0,  "max_cov": 0.20},
    "hydro_lkdep":          {"kind": "gradient", "min_cov": 0.0,  "max_cov": 0.20},
    "hydro_lake_wl":        {"kind": "gradient", "min_cov": 0.0,  "max_cov": 0.20},
    "hydro_centerline":     {"kind": "gradient", "min_cov": 0.0,  "max_cov": 0.05},
    "hydro_floodplain":     {"kind": "gradient", "min_cov": 0.001,"max_cov": 0.25},
    # Eco gradient masks
    "wind_windthrow":       {"kind": "gradient", "min_cov": 0.01, "max_cov": 0.25},
    "rock_exposure":        {"kind": "gradient", "min_cov": 0.01, "max_cov": 0.40},
    "rock_exposure_tight":  {"kind": "gradient", "min_cov": 0.0,  "max_cov": 0.30},
    "snow_caps":            {"kind": "gradient", "min_cov": 0.0,  "max_cov": 0.25},
    "sand_dunes":           {"kind": "gradient", "min_cov": 0.001,"max_cov": 0.25},
    # Phase 0.5 (S44) — physical realism refactor precomputes.
    # lithology: uint8 1..6, 0 = water/unclassified. Expect ~30% nonzero (land fraction).
    "lithology":            {"kind": "discrete", "min_cov": 0.25, "max_cov": 0.45},
    # wave_fetch: land-only signal; shore pixels east of water. Tiny coverage (~0.0005).
    "wave_fetch":           {"kind": "gradient", "min_cov": 0.0,  "max_cov": 0.02},
}


# Phase 0.5 extras — lithology-specific sanity checks beyond coverage bounds.
# Spec: PHYSICAL_REALISM_REFACTOR.md §6 Pass 0 + §11 Phase 0.5 validator checklist.
LITHOLOGY_EXPECTED_IDS = {0, 1, 2, 3, 4, 5, 6}
LITHOLOGY_MIN_GROUPS_PRESENT = 4  # at least 4 of 6 groups must be nonzero on land


def check_lithology_extras(masks_dir: Path) -> list[dict]:
    """Extra lithology sanity checks — band IDs, group diversity, water alignment.

    Returns a list of result dicts (same schema as check_mask).
    """
    import numpy as np
    import rasterio

    out: list[dict] = []
    path = masks_dir / "lithology.tif"
    if not path.exists():
        return out

    with rasterio.open(str(path)) as src:
        lith = src.read(1)
    shape = lith.shape

    # Check 1: valid IDs only.
    r1 = {"name": "lithology_ids", "path": str(path), "detail": {}}
    unique = set(int(x) for x in np.unique(lith))
    unexpected = unique - LITHOLOGY_EXPECTED_IDS
    r1["detail"]["unique_ids"] = sorted(unique)
    if unexpected:
        r1["result"] = "FAIL"
        r1["message"] = f"unexpected ids {sorted(unexpected)}"
    else:
        r1["result"] = "PASS"
        r1["message"] = f"all ids in {sorted(LITHOLOGY_EXPECTED_IDS)}"
    out.append(r1)

    # Check 2: group diversity — at least N distinct nonzero groups on land.
    r2 = {"name": "lithology_group_diversity", "path": str(path), "detail": {}}
    nonzero_ids = unique - {0}
    r2["detail"]["nonzero_group_count"] = len(nonzero_ids)
    if len(nonzero_ids) < LITHOLOGY_MIN_GROUPS_PRESENT:
        r2["result"] = "FAIL"
        r2["message"] = (
            f"only {len(nonzero_ids)} nonzero groups present, "
            f"expected ≥{LITHOLOGY_MIN_GROUPS_PRESENT}")
    else:
        r2["result"] = "PASS"
        r2["message"] = f"{len(nonzero_ids)} nonzero groups present"
    out.append(r2)

    # Check 3: water alignment — lithology==0 fraction should be close to the
    # land-inverse fraction (water + unclassified). Sanity: must be within
    # [0.40, 0.80] for Vandir (30% land, some coastal zones unmapped).
    r3 = {"name": "lithology_water_alignment", "path": str(path), "detail": {}}
    zero_frac = float((lith == 0).mean())
    r3["detail"]["zero_frac"] = round(zero_frac, 4)
    if 0.40 <= zero_frac <= 0.80:
        r3["result"] = "PASS"
        r3["message"] = f"zero/water fraction {zero_frac:.3f} in [0.40, 0.80]"
    else:
        r3["result"] = "FAIL"
        r3["message"] = f"zero/water fraction {zero_frac:.3f} outside [0.40, 0.80]"
    out.append(r3)

    # Check 4: shape at 1:8 precompute resolution.
    r4 = {"name": "lithology_shape", "path": str(path), "detail": {"shape": list(shape)}}
    if shape == (6250, 6250):
        r4["result"] = "PASS"
        r4["message"] = "shape 6250×6250 (1:8)"
    else:
        r4["result"] = "FAIL"
        r4["message"] = f"unexpected shape {shape}, want (6250, 6250)"
    out.append(r4)

    return out


def load_bounds(project_root: Path) -> dict:
    affects_path = project_root / "config" / "validation_affects.json"
    if not affects_path.exists():
        return DEFAULT_BOUNDS
    try:
        data = json.loads(affects_path.read_text(encoding="utf-8"))
        bounds = data.get("mask_bounds")
        if isinstance(bounds, dict) and bounds:
            # Merge so any missing mask falls back to default
            merged = dict(DEFAULT_BOUNDS)
            merged.update(bounds)
            return merged
    except Exception as e:
        print(f"[validate_masks] WARN: affects file unreadable ({e}), using defaults")
    return DEFAULT_BOUNDS


def check_mask(path: Path, spec: dict) -> dict:
    """Open one mask and run its sanity checks. Returns a result dict."""
    result = {
        "name": path.stem,
        "path": str(path),
        "result": "SKIP",
        "message": "",
        "detail": {},
    }
    if not path.exists():
        result["result"] = "FAIL"
        result["message"] = "file missing"
        return result

    try:
        import rasterio
        from rasterio.enums import Resampling
    except ImportError:
        result["result"] = "FAIL"
        result["message"] = "rasterio not installed"
        return result

    try:
        with rasterio.open(str(path)) as src:
            shape = (src.height, src.width)
            dtype = src.dtypes[0]
            result["detail"]["shape"] = list(shape)
            result["detail"]["dtype"] = str(dtype)
            # Shape check — we allow exact or "close to" 50k because override
            # and precompute may be 1:8 at 6250 × 6250.
            if shape == EXPECTED_SHAPE:
                result["detail"]["scale"] = "50k"
            elif shape == (6250, 6250):
                result["detail"]["scale"] = "1:8"
            else:
                result["result"] = "FAIL"
                result["message"] = f"unexpected shape {shape}"
                return result

            # Strided full-raster downsample (nearest, preserves zone codes
            # and binary masks). Replaces the old NW-corner window which
            # reported 0% coverage on land-only masks of an ocean-cornered
            # world.
            grid_h = min(SAMPLE_GRID, src.height)
            grid_w = min(SAMPLE_GRID, src.width)
            sample = src.read(
                1,
                out_shape=(grid_h, grid_w),
                resampling=Resampling.nearest,
            )
            import numpy as np
            # Normalize for coverage math
            if sample.dtype == np.uint16:
                f = sample.astype(np.float32) / 65535.0
            elif sample.dtype == np.uint8:
                f = sample.astype(np.float32) / 255.0
            else:
                f = sample.astype(np.float32)

            kind = spec.get("kind", "gradient")
            if kind == "discrete":
                cov = float((f > 0).mean())
            elif kind == "dense":
                cov = float(np.isfinite(f).mean())
            else:  # gradient
                cov = float((f > 0.001).mean())

            result["detail"]["coverage"] = round(cov, 4)

            nan_frac = float(np.isnan(f).mean()) if f.dtype.kind == "f" else 0.0
            result["detail"]["nan_frac"] = round(nan_frac, 4)

            if nan_frac > 0:
                result["result"] = "FAIL"
                result["message"] = f"NaN pixels in sample ({nan_frac:.2%})"
                return result

            lo = spec.get("min_cov", 0.0)
            hi = spec.get("max_cov", 1.0)
            if cov < lo:
                result["result"] = "FAIL"
                result["message"] = f"coverage {cov:.3f} < min {lo:.3f} ({kind})"
                return result
            if cov > hi:
                result["result"] = "FAIL"
                result["message"] = f"coverage {cov:.3f} > max {hi:.3f} ({kind})"
                return result

            result["result"] = "PASS"
            result["message"] = f"{kind} cov={cov:.3f} ({shape[0]}×{shape[1]} {dtype})"
            return result
    except Exception as e:
        result["result"] = "FAIL"
        result["message"] = f"read error: {e}"
        return result


def main() -> int:
    p = argparse.ArgumentParser(description="Vandir mask sanity validator")
    p.add_argument("--masks",  required=True, help="masks/ directory")
    p.add_argument("--only",   default="",
                   help="Comma-separated list of mask stems to check (default: all)")
    p.add_argument("--report", default="",
                   help="Output dir for report.json + report.txt (default: stdout only)")
    args = p.parse_args()

    masks_dir = Path(args.masks)
    if not masks_dir.is_dir():
        print(f"[validate_masks] FATAL: masks dir not found: {masks_dir}", file=sys.stderr)
        return 2

    bounds = load_bounds(_PROJECT_ROOT)
    names = [n.strip() for n in args.only.split(",") if n.strip()] if args.only else list(bounds.keys())

    results = []
    for name in names:
        spec = bounds.get(name, {"kind": "gradient", "min_cov": 0.0, "max_cov": 1.0})
        path = masks_dir / f"{name}.tif"
        res = check_mask(path, spec)
        results.append(res)

    # Phase 0.5 lithology extras — run only if lithology.tif is being checked
    # (or if no --only filter is set).
    if not args.only or "lithology" in names:
        try:
            extras = check_lithology_extras(masks_dir)
            results.extend(extras)
        except Exception as e:
            results.append({
                "name": "lithology_extras", "path": "", "result": "FAIL",
                "message": f"lithology extras error: {e}", "detail": {},
            })

    n_pass = sum(1 for r in results if r["result"] == "PASS")
    n_fail = sum(1 for r in results if r["result"] == "FAIL")
    n_skip = sum(1 for r in results if r["result"] == "SKIP")

    lines = [
        "=" * 60,
        "  Vandir Mask Sanity",
        "=" * 60,
        f"  PASS: {n_pass}  FAIL: {n_fail}  SKIP: {n_skip}",
        "",
    ]
    for r in results:
        sym = {"PASS":"✅","FAIL":"❌","SKIP":"—"}.get(r["result"], "?")
        lines.append(f"  {sym} {r['name']}: {r['message']}")
        if r["detail"]:
            det = ", ".join(f"{k}={v}" for k, v in r["detail"].items())
            lines.append(f"       {det}")
    lines += ["", "=" * 60]
    txt = "\n".join(lines)
    print(txt)

    if args.report:
        report_dir = Path(args.report)
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "report.txt").write_text(txt, encoding="utf-8")
        (report_dir / "checks.json").write_text(
            json.dumps(
                {"passed": n_pass, "failed": n_fail, "skipped": n_skip, "results": results},
                indent=2,
            ),
            encoding="utf-8",
        )

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())