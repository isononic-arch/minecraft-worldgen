#!/usr/bin/env python3
"""
river_pass.py — Vandir Automated River Pass
============================================
Reads Erosion2_Flow.tif and Erosion2_Out.tif, detects main river channels,
and paints val 80 (Riparian Woodland) into override_final.png.

River width is variable:
  - Driven by flow intensity (high flow = wider)
  - Modulated by elevation (low elevation = wider, high = narrower)
  - Minimum connected-length filter removes noise

Thresholds (from Panel 4 GUI testing):
  - Flow threshold:  10% normalized  → river channel detection
  - Land threshold:  15% normalized  → land mask (exclude ocean)

Usage:
  py river_pass.py           → apply river pass to override_final.png
  py river_pass.py --preview → generate preview PNG only, no file modification

Preview output: river_pass_preview.png
  - Existing override zones shown in greyscale
  - NEW river pixels shown in bright blue
  - Ocean shown in black
  - Existing val 80 pixels shown in cyan (already painted)

Dependencies:
  py -m pip install numpy rasterio pillow scipy
"""

import argparse
import numpy as np
import rasterio
from rasterio.windows import Window
from PIL import Image
from scipy import ndimage
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG — edit paths if needed
# ---------------------------------------------------------------------------

OVERRIDE_PATH   = r"C:\Users\nicho\minecraft-worldgen\override_final.png"
FLOW_PATH       = r"C:\Gaea Stuff\Erosion2_Flow.tif"
HEIGHT_PATH     = r"C:\Gaea Stuff\Erosion2_Out.tif"
OUTPUT_PATH     = r"C:\Users\nicho\minecraft-worldgen\override_final.png"  # overwrites in place
BACKUP_PATH     = r"C:\Users\nicho\minecraft-worldgen\override_final_preriver.png"

RIVER_VAL       = 80       # override value to paint
FLOW_THRESHOLD  = 0.10     # 10% — river channel detection
LAND_THRESHOLD  = 0.15     # 15% — land mask (below = ocean, skip)

# River width in source pixels by flow intensity bucket
# (flow_min, flow_max, width_px)
FLOW_WIDTH_BUCKETS = [
    (0.10, 0.25, 2),   # headwaters / minor tributaries
    (0.25, 0.50, 3),   # mid-catchment
    (0.50, 0.75, 5),   # main tributaries
    (0.75, 1.00, 7),   # trunk rivers
]

# Elevation modulates width: low elevation multiplier, high elevation multiplier
ELEV_LOW_THRESHOLD  = 0.25   # below this normalized height = low elevation
ELEV_HIGH_THRESHOLD = 0.60   # above this normalized height = high elevation
ELEV_LOW_MULT       = 1.4    # widen rivers in lowlands
ELEV_HIGH_MULT      = 0.6    # narrow rivers near peaks

# Minimum connected river pixel length (removes noise blobs)
MIN_RIVER_LENGTH    = 50     # pixels

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def load_tif_normalized(path: str) -> np.ndarray:
    """Load a TIFF16 file and normalize to [0, 1]."""
    with rasterio.open(path) as src:
        data = src.read(1).astype(np.float32)
    data_min = data.min()
    data_max = data.max()
    if data_max == data_min:
        return np.zeros_like(data)
    return (data - data_min) / (data_max - data_min)


def get_river_width(flow_val: float, elev_val: float) -> int:
    """
    Compute river width in pixels based on flow intensity and elevation.
    """
    # Base width from flow bucket
    base_width = 2
    for fmin, fmax, width in FLOW_WIDTH_BUCKETS:
        if fmin <= flow_val < fmax:
            base_width = width
            break
    # Handle max flow edge case
    if flow_val >= 1.0:
        base_width = FLOW_WIDTH_BUCKETS[-1][2]

    # Elevation multiplier
    if elev_val < ELEV_LOW_THRESHOLD:
        mult = ELEV_LOW_MULT
    elif elev_val > ELEV_HIGH_THRESHOLD:
        mult = ELEV_HIGH_MULT
    else:
        # Linear interpolation between low and high
        t = (elev_val - ELEV_LOW_THRESHOLD) / (ELEV_HIGH_THRESHOLD - ELEV_LOW_THRESHOLD)
        mult = ELEV_LOW_MULT + t * (ELEV_HIGH_MULT - ELEV_LOW_MULT)

    return max(1, round(base_width * mult))


# ---------------------------------------------------------------------------
# PREVIEW
# ---------------------------------------------------------------------------

def generate_preview(
    override: np.ndarray,
    river_painted: np.ndarray,
    land_mask: np.ndarray,
    preview_path: str
):
    """
    Generate a false-color preview PNG showing river pass results.
    
    Color key:
      Black       = ocean (not land)
      Grey        = existing override zones (greyscale, brightened for visibility)
      Bright Blue = NEW river pixels added by this pass (val 80, not previously 80)
      Cyan        = existing val 80 pixels (already painted before this pass)
    """
    h, w = override.shape
    preview = np.zeros((h, w, 3), dtype=np.uint8)

    # Ocean — black (default, already zero)

    # Land base — render existing override as brightened greyscale
    land_grey = np.clip(override.astype(np.float32) * 1.8, 0, 255).astype(np.uint8)
    for c in range(3):
        preview[:, :, c] = np.where(land_mask, land_grey, 0)

    # Existing val 80 pixels (painted before this run) — cyan
    existing_80 = land_mask & (override == RIVER_VAL)
    preview[existing_80] = [0, 220, 220]

    # New river pixels added by this pass — bright blue
    new_river = land_mask & (river_painted == RIVER_VAL) & (override != RIVER_VAL)
    preview[new_river] = [30, 100, 255]

    img = Image.fromarray(preview, mode="RGB")
    img.save(preview_path)
    print(f"Preview saved: {preview_path}")
    print(f"  Bright blue pixels (new rivers): {new_river.sum():,}")
    print(f"  Cyan pixels (pre-existing val 80): {existing_80.sum():,}")
    print("  Open the preview PNG to review before running without --preview")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Vandir River Pass")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Generate preview PNG only — does not modify override_final.png"
    )
    args = parser.parse_args()

    if args.preview:
        print("=== PREVIEW MODE — no files will be modified ===")
    else:
        print("=== APPLY MODE — will overwrite override_final.png ===")

    print("Loading masks...")
    flow = load_tif_normalized(FLOW_PATH)
    height = load_tif_normalized(HEIGHT_PATH)

    h, w = flow.shape
    print(f"Mask size: {w}x{h}")

    # Height mask polarity: low Gaea value = high terrain, high value = ocean
    # So land = height < (1 - LAND_THRESHOLD) i.e. NOT the brightest pixels
    land_mask = height < (1.0 - LAND_THRESHOLD)
    print(f"Land pixels: {land_mask.sum():,}")

    # River channel detection: flow above threshold AND on land
    river_candidates = (flow >= FLOW_THRESHOLD) & land_mask
    print(f"River candidate pixels before length filter: {river_candidates.sum():,}")

    # Label connected components and filter by minimum length
    labeled, num_features = ndimage.label(river_candidates)
    print(f"Connected river components: {num_features}")

    component_sizes = ndimage.sum(river_candidates, labeled, range(1, num_features + 1))
    valid_labels = np.where(np.array(component_sizes) >= MIN_RIVER_LENGTH)[0] + 1
    print(f"Components passing length filter (>={MIN_RIVER_LENGTH}px): {len(valid_labels)}")

    # Build filtered river mask
    river_mask = np.isin(labeled, valid_labels)
    print(f"River pixels after length filter: {river_mask.sum():,}")

    # Load override image
    print(f"Loading override: {OVERRIDE_PATH}")
    override_img = Image.open(OVERRIDE_PATH).convert("L")
    override = np.array(override_img, dtype=np.uint8)

    # Back up original
    print(f"Saving backup to: {BACKUP_PATH}")
    override_img.save(BACKUP_PATH)

    # Paint rivers with variable width
    # Build a dilated river layer where each pixel's dilation radius
    # depends on its flow + elevation value
    print("Painting rivers with variable width...")
    river_painted = override.copy()

    # Get river pixel coordinates
    ys, xs = np.where(river_mask)
    print(f"Processing {len(ys):,} river pixels...")

    # Build a set of all pixels to paint with their max width
    # Use a width map — take the max width at each pixel
    width_map = np.zeros((h, w), dtype=np.uint8)

    for y, x in zip(ys, xs):
        flow_val = float(flow[y, x])
        elev_val = 1.0 - float(height[y, x])  # invert: low Gaea value = high terrain
        px_width = get_river_width(flow_val, elev_val)
        width_map[y, x] = max(width_map[y, x], px_width)

    # For each unique width, dilate and paint
    for px_width in range(1, 9):
        pixels_this_width = width_map >= px_width
        if not pixels_this_width.any():
            continue
        # Dilate by 1px for this width level
        struct = ndimage.generate_binary_structure(2, 1)
        dilated = ndimage.binary_dilation(
            pixels_this_width,
            structure=struct,
            iterations=px_width
        )
        # Only paint on land pixels
        to_paint = dilated & land_mask
        river_painted[to_paint] = RIVER_VAL

    painted_count = int(np.sum(river_painted == RIVER_VAL)) - int(np.sum(override == RIVER_VAL))
    print(f"New river pixels painted: {painted_count:,}")

    if args.preview:
        # Preview mode — generate false-color PNG, do not touch override
        preview_path = str(Path(OVERRIDE_PATH).parent / "river_pass_preview.png")
        generate_preview(override, river_painted, land_mask, preview_path)
        print("\nPreview complete. No files modified.")
        print("Run without --preview to apply when satisfied.")
    else:
        # Apply mode — back up and save
        print(f"Saving backup to: {BACKUP_PATH}")
        Image.fromarray(override, mode="L").save(BACKUP_PATH)
        print(f"Saving output to: {OUTPUT_PATH}")
        result = Image.fromarray(river_painted, mode="L")
        result.save(OUTPUT_PATH)
        print("Done. override_final.png updated with river pass.")
        print(f"Backup saved at: {BACKUP_PATH}")


if __name__ == "__main__":
    main()
