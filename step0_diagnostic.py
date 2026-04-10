#!/usr/bin/env python3
"""
step0_diagnostic.py — Vandir World Generation Pipeline
=======================================================
Hard prerequisite for all subsequent pipeline steps.

Outputs:
  - Sea level threshold (16-bit height value where ocean surface sits)
  - Land / ocean pixel percentages
  - Recommended piecewise spline breakpoints for height remapping
  - Water mask alignment validation
  - step0_output.json  ← consumed by downstream steps

Usage:
  python step0_diagnostic.py --height masks/height.png --water masks/water.png

Accepts PNG inputs (pre-conversion not required for diagnostics).
All reads are chunked — no full raster loads.

Dependencies:
  pip install numpy rasterio matplotlib scipy
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
import matplotlib
matplotlib.use("Agg")  # headless — no display required
import matplotlib.pyplot as plt
from scipy.interpolate import PchipInterpolator


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CHUNK_SIZE = 1024          # rows read per pass (RAM-safe for 8k or 50k inputs)
HISTOGRAM_BINS = 65536     # full 16-bit range
OUTPUT_JSON = "step0_output.json"
OUTPUT_HISTOGRAM_PNG = "step0_height_histogram.png"
OUTPUT_ALIGNMENT_PNG = "step0_water_alignment.png"

# Minecraft Y range (Higher Heights datapack)
MC_Y_MIN = -64
MC_Y_MAX = 448
MC_SEA_LEVEL = 63


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_histogram(path: Path, bins: int = HISTOGRAM_BINS) -> np.ndarray:
    """
    Build a full histogram of a 16-bit grayscale raster without loading
    the entire file into RAM. Reads in horizontal strips of CHUNK_SIZE rows.

    Args:
        path: Path to raster file (PNG or TIF).
        bins: Number of histogram bins (65536 for full 16-bit).

    Returns:
        1-D numpy array of length `bins` with pixel counts.
    """
    hist = np.zeros(bins, dtype=np.int64)

    with rasterio.open(path) as src:
        width = src.width
        height = src.height
        print(f"  [{path.name}] size: {width}×{height}, dtype: {src.dtypes[0]}, "
              f"bands: {src.count}")

        for row_off in range(0, height, CHUNK_SIZE):
            rows = min(CHUNK_SIZE, height - row_off)
            window = Window(0, row_off, width, rows)
            data = src.read(1, window=window).astype(np.int64)
            chunk_hist, _ = np.histogram(data, bins=bins, range=(0, bins))
            hist += chunk_hist

    return hist


def find_sea_level_threshold(hist: np.ndarray) -> int:
    """
    Estimate the sea level threshold from a height histogram.

    Strategy: oceans appear as a large flat region in the histogram at
    low values. We find the local minimum in cumulative density that
    separates the ocean plateau from the land mass distribution.

    Returns the 16-bit value at which ocean surface sits (~63 MC Y).
    """
    total_pixels = hist.sum()
    cumsum = np.cumsum(hist).astype(np.float64) / total_pixels

    # Smooth histogram to find inflection
    from scipy.ndimage import uniform_filter1d
    smoothed = uniform_filter1d(hist.astype(np.float64), size=256)
    gradient = np.gradient(smoothed)

    # Ocean floor region: values 0–~15000 typically
    # Sea level plateau: sharp spike around the threshold
    # Strategy: find the value where cumulative % transitions from
    # flat (ocean floor) to rising steeply (land begins).
    # Look for the first local gradient maximum below value 30000.
    search_range = slice(1000, 35000)
    local_max_idx = int(np.argmax(gradient[search_range])) + 1000

    # Snap to nearest bin with high count (the actual sea surface plateau)
    window_start = max(0, local_max_idx - 500)
    window_end = min(HISTOGRAM_BINS, local_max_idx + 500)
    peak_in_window = int(np.argmax(hist[window_start:window_end])) + window_start

    return peak_in_window


def compute_land_ocean_split(hist: np.ndarray, sea_threshold: int) -> dict:
    """
    Calculate land vs ocean pixel percentages given a sea level threshold.

    Args:
        hist: Full 16-bit histogram.
        sea_threshold: 16-bit value of sea surface.

    Returns:
        Dict with ocean_pct, land_pct, total_pixels.
    """
    total = hist.sum()
    ocean_pixels = hist[:sea_threshold].sum()
    land_pixels = hist[sea_threshold:].sum()
    return {
        "total_pixels": int(total),
        "ocean_pixels": int(ocean_pixels),
        "land_pixels": int(land_pixels),
        "ocean_pct": round(float(ocean_pixels) / total * 100, 2),
        "land_pct": round(float(land_pixels) / total * 100, 2),
    }


def compute_spline_breakpoints(sea_threshold: int) -> dict:
    """
    Compute piecewise spline breakpoints for Gaea 16-bit → MC Y remapping.

    Curve shape:
      0           → -64   (deepest ocean floor)
      sea_threshold → 63  (sea level)
      45000       → 200   (mid-mountain)
      65535       → 448   (max peak)

    Args:
        sea_threshold: 16-bit sea level value from histogram analysis.

    Returns:
        Dict of breakpoints {gaea_value: mc_y} and spline sample table.
    """
    gaea_x = [0, sea_threshold, 45000, 65535]
    mc_y   = [MC_Y_MIN, MC_SEA_LEVEL, 200, MC_Y_MAX]

    spline = PchipInterpolator(gaea_x, mc_y)

    # Sample 256 points for the lookup table embedded in step0_output.json
    sample_x = np.linspace(0, 65535, 256).astype(int)
    sample_y = np.clip(spline(sample_x), MC_Y_MIN, MC_Y_MAX).astype(int).tolist()

    return {
        "breakpoints": {
            "gaea": gaea_x,
            "mc_y": mc_y,
        },
        "lut_256": {
            "gaea_values": sample_x.tolist(),
            "mc_y_values": sample_y,
        }
    }


def validate_water_mask_alignment(
    height_path: Path,
    water_path: Path,
    sea_threshold: int,
    sample_rows: int = 4,
) -> dict:
    """
    Validate that the water mask agrees with the height threshold.

    Samples `sample_rows` horizontal strips, compares:
      - pixels where height < sea_threshold  (predicted ocean by height)
      - pixels where water mask > 32767      (ocean per water mask)

    Returns alignment statistics.
    """
    results = []

    with rasterio.open(height_path) as h_src, rasterio.open(water_path) as w_src:
        if h_src.width != w_src.width or h_src.height != w_src.height:
            return {
                "error": (
                    f"SIZE MISMATCH — height {h_src.width}×{h_src.height} "
                    f"vs water {w_src.width}×{w_src.height}. "
                    "Ensure both masks are exported at the same resolution."
                )
            }

        total_height = h_src.height
        step = total_height // (sample_rows + 1)

        for i in range(1, sample_rows + 1):
            row_off = i * step
            rows = min(CHUNK_SIZE, total_height - row_off)
            window = Window(0, row_off, h_src.width, rows)

            h_data = h_src.read(1, window=window)
            w_data = w_src.read(1, window=window)

            height_ocean = h_data < sea_threshold
            water_ocean  = w_data > 32767   # upper half of 16-bit = ocean

            agreement = np.logical_not(np.logical_xor(height_ocean, water_ocean))
            pct = float(agreement.mean()) * 100

            results.append({
                "sample_row": int(row_off),
                "agreement_pct": round(pct, 2),
                "height_ocean_pct": round(float(height_ocean.mean()) * 100, 2),
                "water_ocean_pct":  round(float(water_ocean.mean())  * 100, 2),
            })

    overall = np.mean([r["agreement_pct"] for r in results])
    passed = overall >= 90.0

    return {
        "passed": passed,
        "overall_agreement_pct": round(float(overall), 2),
        "warning": None if passed else (
            f"Alignment only {overall:.1f}% — water mask may not match height threshold "
            f"{sea_threshold}. Review masks in Gaea before proceeding."
        ),
        "samples": results,
    }


def plot_histogram(hist: np.ndarray, sea_threshold: int, out_path: str) -> None:
    """Save an annotated histogram PNG for human review."""
    fig, ax = plt.subplots(figsize=(14, 5))

    # Plot log-scale histogram (skip bin 0 — pure black pixels)
    x = np.arange(1, HISTOGRAM_BINS)
    ax.semilogy(x, hist[1:] + 1, color="#4a9eff", linewidth=0.8, label="pixel count")

    ax.axvline(sea_threshold, color="#ff4444", linewidth=1.5,
               label=f"sea threshold: {sea_threshold}")
    ax.axvline(45000, color="#ffaa00", linewidth=1.0, linestyle="--",
               label="45000 (mid-mountain breakpoint)")

    ax.set_xlabel("16-bit Gaea height value (0–65535)")
    ax.set_ylabel("pixel count (log scale)")
    ax.set_title("Vandir — Height Mask Histogram (Step 0)")
    ax.legend(fontsize=9)
    ax.set_xlim(0, 65535)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150)
    plt.close()
    print(f"  Histogram saved → {out_path}")


def plot_alignment_preview(
    height_path: Path,
    water_path: Path,
    sea_threshold: int,
    out_path: str,
) -> None:
    """
    Save a side-by-side comparison image of height-derived ocean mask
    vs water mask for a central crop (1024×1024 pixels).
    """
    with rasterio.open(height_path) as h_src:
        cx = h_src.width  // 2 - 512
        cy = h_src.height // 2 - 512
        window = Window(cx, cy, 1024, 1024)
        h_data = h_src.read(1, window=window)

    with rasterio.open(water_path) as w_src:
        w_data = w_src.read(1, window=window)

    height_ocean = (h_data < sea_threshold).astype(np.uint8) * 255
    water_ocean  = (w_data > 32767).astype(np.uint8) * 255

    fig, axes = plt.subplots(1, 2, figsize=(12, 6))
    axes[0].imshow(height_ocean, cmap="Blues", vmin=0, vmax=255)
    axes[0].set_title(f"Height < {sea_threshold} (predicted ocean)")
    axes[0].axis("off")

    axes[1].imshow(water_ocean, cmap="Blues", vmin=0, vmax=255)
    axes[1].set_title("Water mask > 32767 (declared ocean)")
    axes[1].axis("off")

    plt.suptitle("Water Mask Alignment Preview — centre 1024×1024px crop", y=1.01)
    plt.tight_layout()
    plt.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close()
    print(f"  Alignment preview saved → {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Step 0 — Vandir diagnostic script. Must run before any pipeline step."
    )
    parser.add_argument(
        "--height", required=True,
        help="Path to height mask (PNG or Tiled BigTIFF)"
    )
    parser.add_argument(
        "--water", required=True,
        help="Path to water/ocean mask (PNG or Tiled BigTIFF)"
    )
    parser.add_argument(
        "--out", default=OUTPUT_JSON,
        help=f"Output JSON path (default: {OUTPUT_JSON})"
    )
    args = parser.parse_args()

    height_path = Path(args.height)
    water_path  = Path(args.water)

    if not height_path.exists():
        print(f"ERROR: height mask not found: {height_path}", file=sys.stderr)
        sys.exit(1)
    if not water_path.exists():
        print(f"ERROR: water mask not found: {water_path}", file=sys.stderr)
        sys.exit(1)

    print("=" * 60)
    print("  Vandir Step 0 — Diagnostic Script")
    print("=" * 60)

    # 1 — Build height histogram
    print("\n[1/5] Building height histogram...")
    hist = build_histogram(height_path)
    print(f"  Total pixels counted: {hist.sum():,}")

    # 2 — Find sea level threshold
    print("\n[2/5] Finding sea level threshold...")
    sea_threshold = find_sea_level_threshold(hist)
    print(f"  Estimated sea threshold: {sea_threshold}  "
          f"(maps to MC Y=63)")

    # 3 — Land / ocean split
    print("\n[3/5] Computing land/ocean split...")
    split = compute_land_ocean_split(hist, sea_threshold)
    print(f"  Ocean: {split['ocean_pct']}%  |  Land: {split['land_pct']}%")

    # 4 — Spline breakpoints
    print("\n[4/5] Computing height remap spline...")
    spline_data = compute_spline_breakpoints(sea_threshold)
    bp = spline_data["breakpoints"]
    print(f"  Breakpoints → Gaea: {bp['gaea']}  MC Y: {bp['mc_y']}")

    # 5 — Water mask alignment
    print("\n[5/5] Validating water mask alignment...")
    alignment = validate_water_mask_alignment(height_path, water_path, sea_threshold)
    if "error" in alignment:
        print(f"  ERROR: {alignment['error']}")
    elif alignment["passed"]:
        print(f"  PASSED — {alignment['overall_agreement_pct']}% agreement")
    else:
        print(f"  WARNING — {alignment['warning']}")

    # Save histogram PNG
    print("\nSaving diagnostic images...")
    plot_histogram(hist, sea_threshold, OUTPUT_HISTOGRAM_PNG)
    try:
        plot_alignment_preview(height_path, water_path, sea_threshold,
                               OUTPUT_ALIGNMENT_PNG)
    except Exception as e:
        print(f"  Alignment preview failed (non-fatal): {e}")

    # Assemble output JSON
    output = {
        "vandir_step0_version": "1.0",
        "inputs": {
            "height_mask": str(height_path),
            "water_mask":  str(water_path),
        },
        "sea_level_threshold_16bit": sea_threshold,
        "mc_sea_level_y": MC_SEA_LEVEL,
        "land_ocean_split": split,
        "height_remap_spline": spline_data,
        "water_mask_alignment": alignment,
        "ready_to_proceed": (
            alignment.get("passed", False)
            and "error" not in alignment
        ),
    }

    def json_safe(obj):
        if isinstance(obj, (np.bool_, np.integer)):
            return obj.item()
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        raise TypeError(f"Not serializable: {type(obj)}")

    with open(args.out, "w") as f:
        json.dump(output, f, indent=2, default=json_safe)

    print(f"\nOutput written → {args.out}")

    # Final status
    print("\n" + "=" * 60)
    if output["ready_to_proceed"]:
        print("  STEP 0 PASSED — safe to proceed to Step 1")
        print(f"  Sea level threshold : {sea_threshold}")
        print(f"  Land / Ocean        : {split['land_pct']}% / {split['ocean_pct']}%")
    else:
        print("  STEP 0 NEEDS REVIEW — check warnings above before proceeding")
        print(f"  Sea level threshold : {sea_threshold}  (review histogram PNG)")
        if "error" in alignment:
            print(f"  Alignment error     : {alignment['error']}")
        elif not alignment.get("passed"):
            print(f"  Alignment           : {alignment.get('overall_agreement_pct')}% "
                  f"(below 90% threshold)")
    print("=" * 60)


if __name__ == "__main__":
    main()
