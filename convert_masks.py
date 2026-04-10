#!/usr/bin/env python3
"""
convert_masks.py — Vandir Pipeline Step 1a
==========================================
Upscales all 8192×8192 Gaea TIFF masks to 50,000×50,000 Tiled BigTIFF
format required by the pipeline tile streamer.

Also derives two masks that Gaea failed to export:
  - shore.tif   — pixels where height < 17050 (sea level threshold)
  - river.tif   — pixels where flow > 0.65 normalised (river channels)
  - override.tif — nearest-neighbour upscale of override_final.png

Outputs go to:  masks/  (relative to CWD, or --output-dir)

Usage:
    py convert_masks.py
    py convert_masks.py --gaea-dir "D:/Gaea Stuff" --override "C:/Users/nicho/override_final.png"
    py convert_masks.py --dry-run   # preview what would be done

Prerequisites:
    pip install rasterio numpy pillow
    GDAL must be on PATH for the gdal_translate calls (optional fast path).
    Falls back to rasterio if GDAL not available.

All locked values from step0_output.json / Project Bible V4:
    SEA_LEVEL_THRESHOLD = 17050  (16-bit)
    RIVER_THRESHOLD     = 0.65   (normalised, Erosion2_Flow bright = rivers)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_bounds

# ── Locked constants ──────────────────────────────────────────────────────────
SEA_LEVEL_THRESHOLD_16BIT: int   = 17050
RIVER_FLOW_THRESHOLD_NORM: float = 0.65   # Erosion2_Flow: bright = rivers
TARGET_SIZE:               int   = 50_000
TILE_BLOCK:                int   = 512

# ── Source mask definitions ───────────────────────────────────────────────────
# (pipeline_name, gaea_filename, resampling, dtype)
# "nearest" for override (never interpolate zone IDs), "lanczos" for all others
MASKS = [
    ("height",   "Erosion2_Out.tif",     "lanczos",  "uint16"),
    ("slope",    "Slope_Out.tif",         "lanczos",  "uint16"),
    ("erosion",  "Erosion2_Wear.tif",     "lanczos",  "uint16"),
    ("flow",     "Erosion2_Flow.tif",     "lanczos",  "uint16"),
    ("deposits", "Erosion2_Deposits.tif", "lanczos",  "uint16"),
]

RESAMPLING_MAP = {
    "lanczos":  Resampling.lanczos,
    "nearest":  Resampling.nearest,
    "bilinear": Resampling.bilinear,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tiff_profile(width: int, height: int, dtype: str = "uint16") -> dict:
    """Standard Tiled BigTIFF profile for all output masks."""
    return {
        "driver":    "GTiff",
        "dtype":     dtype,
        "width":     width,
        "height":    height,
        "count":     1,
        "bigtiff":   "YES",
        "tiled":     True,
        "blockxsize": TILE_BLOCK,
        "blockysize": TILE_BLOCK,
        "compress":  "deflate",
        "predictor": 2,
        # Dummy transform — pipeline uses pixel coords only
        "transform": from_bounds(0, 0, width, height, width, height),
        "crs":       None,
    }


def _gdal_available() -> bool:
    return shutil.which("gdal_translate") is not None


def _upscale_via_gdal(src: Path, dst: Path, size: int, resampling: str) -> bool:
    """Fast path: use gdal_translate for the heavy upscale."""
    r_flag = {
        "lanczos": "lanczos",
        "nearest": "near",
        "bilinear": "bilinear",
    }.get(resampling, "lanczos")

    cmd = [
        "gdal_translate",
        "-of", "GTiff",
        "-co", "TILED=YES",
        "-co", f"BLOCKXSIZE={TILE_BLOCK}",
        "-co", f"BLOCKYSIZE={TILE_BLOCK}",
        "-co", "BIGTIFF=YES",
        "-co", "COMPRESS=DEFLATE",
        "-co", "PREDICTOR=2",
        "-r", r_flag,
        "-outsize", str(size), str(size),
        str(src), str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"    gdal_translate failed: {result.stderr.strip()}")
        return False
    return True


def _upscale_via_rasterio(
    src: Path,
    dst: Path,
    size: int,
    resampling: str,
    dtype: str,
) -> None:
    """Fallback: chunked rasterio upscale. Slower but always available."""
    rs = RESAMPLING_MAP[resampling]
    with rasterio.open(src) as s:
        src_w, src_h = s.width, s.height
        profile = _tiff_profile(size, size, dtype)

        with rasterio.open(dst, "w", **profile) as out:
            # Process in vertical strips to stay RAM-safe
            strip_h = max(1, TILE_BLOCK * 4)  # 2048 rows at a time
            for row_off in range(0, size, strip_h):
                rows = min(strip_h, size - row_off)
                # Map output row range back to source coordinates
                src_row_start = int(row_off * src_h / size)
                src_row_end   = int((row_off + rows) * src_h / size)
                src_rows      = max(1, src_row_end - src_row_start)

                from rasterio.windows import Window
                win_src = Window(0, src_row_start, src_w, src_rows)
                data = s.read(1, window=win_src)

                # Resize strip to output width × rows
                import PIL.Image
                img = PIL.Image.fromarray(data)
                pil_rs = {
                    "lanczos": PIL.Image.LANCZOS,
                    "nearest": PIL.Image.NEAREST,
                    "bilinear": PIL.Image.BILINEAR,
                }.get(resampling, PIL.Image.LANCZOS)
                img_resized = img.resize((size, rows), pil_rs)
                strip_data = np.array(img_resized)

                win_dst = Window(0, row_off, size, rows)
                out.write(strip_data.astype(dtype), 1, window=win_dst)


def upscale_mask(
    src: Path,
    dst: Path,
    size: int,
    resampling: str,
    dtype: str,
    use_gdal: bool,
    dry_run: bool,
) -> None:
    print(f"  {src.name} → {dst.name}  [{resampling}]", end="", flush=True)
    if dry_run:
        print("  [DRY RUN]")
        return
    t0 = time.time()
    if use_gdal:
        ok = _upscale_via_gdal(src, dst, size, resampling)
        if not ok:
            print("\n    Falling back to rasterio...")
            _upscale_via_rasterio(src, dst, size, resampling, dtype)
    else:
        _upscale_via_rasterio(src, dst, size, resampling, dtype)
    print(f"  ({time.time()-t0:.1f}s)")


def derive_shore(height_tif: Path, dst: Path, threshold: int, dry_run: bool) -> None:
    """Derive shore mask: pixels where height > threshold → 65535, else 0.
    
    Polarity confirmed: low raw value = high terrain, high raw = ocean.
    Pixels with raw > sea_threshold are ocean/shore.
    """
    print(f"  Deriving shore.tif  (height > {threshold})", end="", flush=True)
    if dry_run:
        print("  [DRY RUN]")
        return
    t0 = time.time()
    profile = _tiff_profile(TARGET_SIZE, TARGET_SIZE, "uint16")
    from rasterio.windows import Window
    with rasterio.open(height_tif) as src, rasterio.open(dst, "w", **profile) as out:
        strip_h = TILE_BLOCK * 4
        for row_off in range(0, TARGET_SIZE, strip_h):
            rows = min(strip_h, TARGET_SIZE - row_off)
            win = Window(0, row_off, TARGET_SIZE, rows)
            data = src.read(1, window=win)
            shore = np.where(data > threshold, np.uint16(65535), np.uint16(0))
            out.write(shore, 1, window=win)
    print(f"  ({time.time()-t0:.1f}s)")


def derive_river(flow_tif: Path, dst: Path, threshold_norm: float, dry_run: bool) -> None:
    """Derive river mask: pixels where flow > threshold*65535 → 65535, else 0."""
    threshold_16bit = int(threshold_norm * 65535)
    print(f"  Deriving river.tif  (flow > {threshold_norm} → {threshold_16bit})", end="", flush=True)
    if dry_run:
        print("  [DRY RUN]")
        return
    t0 = time.time()
    profile = _tiff_profile(TARGET_SIZE, TARGET_SIZE, "uint16")
    from rasterio.windows import Window
    with rasterio.open(flow_tif) as src, rasterio.open(dst, "w", **profile) as out:
        strip_h = TILE_BLOCK * 4
        for row_off in range(0, TARGET_SIZE, strip_h):
            rows = min(strip_h, TARGET_SIZE - row_off)
            win = Window(0, row_off, TARGET_SIZE, rows)
            data = src.read(1, window=win)
            river = np.where(data > threshold_16bit, np.uint16(65535), np.uint16(0))
            out.write(river, 1, window=win)
    print(f"  ({time.time()-t0:.1f}s)")


def upscale_override(override_png: Path, dst: Path, dry_run: bool) -> None:
    """Upscale 8-bit override PNG to 50k using nearest-neighbour (never interpolate IDs)."""
    print(f"  {override_png.name} → {dst.name}  [nearest]", end="", flush=True)
    if dry_run:
        print("  [DRY RUN]")
        return
    t0 = time.time()
    # Override is 8-bit grayscale PNG — use PIL for simplicity, then write as uint16
    from PIL import Image
    img = Image.open(override_png).convert("L")
    img_big = img.resize((TARGET_SIZE, TARGET_SIZE), Image.NEAREST)
    data = np.array(img_big, dtype=np.uint16)
    profile = _tiff_profile(TARGET_SIZE, TARGET_SIZE, "uint16")
    with rasterio.open(dst, "w", **profile) as out:
        # Write in strips
        strip_h = TILE_BLOCK * 4
        from rasterio.windows import Window
        for row_off in range(0, TARGET_SIZE, strip_h):
            rows = min(strip_h, TARGET_SIZE - row_off)
            win = Window(0, row_off, TARGET_SIZE, rows)
            out.write(data[row_off:row_off+rows, :], 1, window=win)
    print(f"  ({time.time()-t0:.1f}s)")


def verify_output(masks_dir: Path) -> dict:
    """Spot-check all output TIFFs exist and have correct dimensions."""
    expected = ["height.tif", "slope.tif", "erosion.tif", "flow.tif",
                "deposits.tif", "shore.tif", "river.tif", "override.tif"]
    results = {}
    for name in expected:
        path = masks_dir / name
        if not path.exists():
            results[name] = "MISSING"
            continue
        try:
            with rasterio.open(path) as src:
                w, h = src.width, src.height
                if w == TARGET_SIZE and h == TARGET_SIZE:
                    results[name] = f"OK ({w}×{h})"
                else:
                    results[name] = f"WRONG SIZE ({w}×{h}, expected {TARGET_SIZE}×{TARGET_SIZE})"
        except Exception as e:
            results[name] = f"ERROR: {e}"
    return results


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Vandir Step 1a — Upscale Gaea masks to 50k Tiled BigTIFF"
    )
    parser.add_argument(
        "--gaea-dir", default=r"C:\Gaea Stuff",
        help="Directory containing Gaea TIFF exports (default: C:\\Gaea Stuff)"
    )
    parser.add_argument(
        "--override", default=r"C:\Users\nicho\minecraft-worldgen\override_final.png",
        help="Path to override_final.png"
    )
    parser.add_argument(
        "--output-dir", default="masks",
        help="Output directory for upscaled TIFFs (default: masks/)"
    )
    parser.add_argument(
        "--step0-json", default=r"C:\Users\nicho\minecraft-worldgen\step0_output.json",
        help="Path to step0_output.json for sea level threshold"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview operations without writing any files"
    )
    parser.add_argument(
        "--verify-only", action="store_true",
        help="Skip conversion, just verify existing outputs"
    )
    args = parser.parse_args()

    gaea_dir   = Path(args.gaea_dir)
    override   = Path(args.override)
    masks_dir  = Path(args.output_dir)
    step0_json = Path(args.step0_json)

    print("=" * 60)
    print("  Vandir Step 1a — convert_masks.py")
    print("=" * 60)

    # Load sea level threshold from step0 JSON if available
    sea_threshold = SEA_LEVEL_THRESHOLD_16BIT
    if step0_json.exists():
        with open(step0_json) as f:
            s0 = json.load(f)
        sea_threshold = s0.get("sea_level_threshold_16bit", SEA_LEVEL_THRESHOLD_16BIT)
        print(f"  Sea level threshold from step0: {sea_threshold}")
    else:
        print(f"  step0_output.json not found — using locked default: {sea_threshold}")

    if args.verify_only:
        print(f"\nVerifying {masks_dir}/...")
        results = verify_output(masks_dir)
        all_ok = True
        for name, status in results.items():
            icon = "✓" if status.startswith("OK") else "✗"
            print(f"  {icon} {name:20s} {status}")
            if not status.startswith("OK"):
                all_ok = False
        print(f"\n{'All masks OK — ready for pipeline.' if all_ok else 'Some masks missing or incorrect.'}")
        sys.exit(0 if all_ok else 1)

    # Validate inputs
    errors = []
    for _, fname, _, _ in MASKS:
        p = gaea_dir / fname
        if not p.exists():
            errors.append(f"Missing: {p}")
    if not override.exists():
        errors.append(f"Missing override: {override}")
    if errors:
        print("\nERROR — missing input files:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)

    if not args.dry_run:
        masks_dir.mkdir(parents=True, exist_ok=True)

    use_gdal = _gdal_available()
    print(f"\n  GDAL available: {use_gdal}")
    print(f"  Output dir:     {masks_dir.resolve()}")
    print(f"  Target size:    {TARGET_SIZE}×{TARGET_SIZE}")
    print(f"  Dry run:        {args.dry_run}")
    print()

    # ── Step 1: Upscale primary masks ─────────────────────────────────────────
    print("[1/3] Upscaling primary masks...")
    for pipeline_name, gaea_fname, resampling, dtype in MASKS:
        src = gaea_dir / gaea_fname
        dst = masks_dir / f"{pipeline_name}.tif"
        if dst.exists() and not args.dry_run:
            print(f"  {dst.name} already exists — skipping (delete to re-run)")
            continue
        upscale_mask(src, dst, TARGET_SIZE, resampling, dtype, use_gdal, args.dry_run)

    # ── Step 2: Upscale override ──────────────────────────────────────────────
    print("\n[2/3] Upscaling override mask...")
    dst_override = masks_dir / "override.tif"
    if dst_override.exists() and not args.dry_run:
        print(f"  override.tif already exists — skipping (delete to re-run)")
    else:
        upscale_override(override, dst_override, args.dry_run)

    # ── Step 3: Derive shore + river ──────────────────────────────────────────
    print("\n[3/3] Deriving shore and river masks...")
    height_tif = masks_dir / "height.tif"
    flow_tif   = masks_dir / "flow.tif"
    shore_tif  = masks_dir / "shore.tif"
    river_tif  = masks_dir / "river.tif"

    if not args.dry_run and not height_tif.exists():
        print("  ERROR: height.tif not found — run primary mask upscale first")
        sys.exit(1)
    if not args.dry_run and not flow_tif.exists():
        print("  ERROR: flow.tif not found — run primary mask upscale first")
        sys.exit(1)

    if shore_tif.exists() and not args.dry_run:
        print(f"  shore.tif already exists — skipping")
    else:
        derive_shore(height_tif, shore_tif, sea_threshold, args.dry_run)

    if river_tif.exists() and not args.dry_run:
        print(f"  river.tif already exists — skipping")
    else:
        derive_river(flow_tif, river_tif, RIVER_FLOW_THRESHOLD_NORM, args.dry_run)

    # ── Verify ────────────────────────────────────────────────────────────────
    if not args.dry_run:
        print("\n[Verify] Checking outputs...")
        results = verify_output(masks_dir)
        all_ok = True
        for name, status in results.items():
            icon = "✓" if status.startswith("OK") else "✗"
            print(f"  {icon} {name:20s} {status}")
            if not status.startswith("OK"):
                all_ok = False

        print("\n" + "=" * 60)
        if all_ok:
            print("  STEP 1a COMPLETE — all 8 masks ready in masks/")
            print("  Run validate_schematics.py next.")
        else:
            print("  STEP 1a INCOMPLETE — fix errors above before proceeding")
        print("=" * 60)
        sys.exit(0 if all_ok else 1)
    else:
        print("\nDry run complete — no files written.")


if __name__ == "__main__":
    main()
