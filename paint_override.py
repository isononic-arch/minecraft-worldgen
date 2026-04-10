#!/usr/bin/env python3
"""
paint_override.py — Vandir Automated Override Mask Painter
===========================================================
Reads the height TIFF from Step 0, detects landmasses via connected
component analysis, lets you label each continent via a simple GUI,
then auto-paints:
  - Continental base biome override values
  - Altitude zones (alpine meadow, arctic tundra) by height threshold
  - Coastal proximity reference mask (for manual refinement)

Outputs:
  - override_base.png        — 8-bit grayscale override mask (pipeline input)
  - coastal_reference.png    — coastal proximity mask for manual painting reference
  - landmass_labeled.png     — false-color labeled landmass map for reference

Usage:
  py paint_override.py --height "C:/Gaea Stuff/Erosion2_Out.tif" --step0 step0_output.json

Dependencies:
  py -m pip install numpy rasterio matplotlib scipy scikit-image pillow
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
import matplotlib
matplotlib.use("TkAgg")  # GUI backend
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.widgets import Button
from scipy import ndimage
from skimage import measure, morphology

# ---------------------------------------------------------------------------
# Override value map (from Project Bible)
# ---------------------------------------------------------------------------

CONTINENT_OPTIONS = {
    "Northern Continent": {
        "base": 30,   # Boreal Taiga
        "description": "Cold, rugged, mountainous — top center"
    },
    "Western/Main Continent": {
        "base": 60,   # Temperate Deciduous
        "description": "Temperate, mixed deciduous — middle left"
    },
    "Eastern Continent": {
        "base": 120,  # Mixed Forest
        "description": "Cool temperate, largest landmass — right center"
    },
    "Southern Continent": {
        "base": 160,  # Lush Rainforest Coast
        "description": "Most diverse, lush west — bottom left"
    },
    "Small Island": {
        "base": 240,  # Freshwater Fen
        "description": "Scattered islands — fen/alder carr"
    },
    "Skip (ocean/ignore)": {
        "base": 0,
        "description": "Not a landmass"
    },
}

# Altitude zone overrides (applied on top of continental base)
ALTITUDE_ZONES = [
    {"name": "Arctic Tundra",  "value": 50, "mc_y_min": 220, "mc_y_max": 999},
    {"name": "Alpine Meadow",  "value": 40, "mc_y_min": 160, "mc_y_max": 220},
]

CHUNK_SIZE = 512


# ---------------------------------------------------------------------------
# Step 0 output loader
# ---------------------------------------------------------------------------

def load_step0(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Height loading (chunked, downsampled for GUI)
# ---------------------------------------------------------------------------

def load_height_downsampled(path: Path, target_size: int = 1024) -> tuple:
    """
    Load height raster downsampled to target_size for GUI display.
    Returns (height_array_small, scale_factor, original_size, dtype_max).
    """
    with rasterio.open(path) as src:
        orig_w = src.width
        orig_h = src.height
        dtype = src.dtypes[0]

        # Calculate downsample factor
        scale = max(orig_w, orig_h) / target_size
        out_w = int(orig_w / scale)
        out_h = int(orig_h / scale)

        print(f"  Loading {orig_w}×{orig_h} → downsampled to {out_w}×{out_h} for GUI")

        # Read with resampling
        data = src.read(
            1,
            out_shape=(out_h, out_w),
            resampling=rasterio.enums.Resampling.average
        )

    dtype_max = 65535 if "uint16" in dtype or "float" in dtype else 255
    return data.astype(np.float32), scale, (orig_w, orig_h), dtype_max


# ---------------------------------------------------------------------------
# Land mask + connected components
# ---------------------------------------------------------------------------

def detect_landmasses(
    height_small: np.ndarray,
    sea_threshold_16bit: int,
    dtype_max: float,
    min_island_pixels: int = 50,
) -> tuple:
    """
    Threshold height to get land mask, run connected components.
    Returns (land_mask, labeled_array, num_components, component_sizes).
    """
    # Normalize threshold to match downsampled data range
    threshold_norm = sea_threshold_16bit
    land_mask = height_small > threshold_norm
    print(f"DEBUG: height min={height_small.min():.1f} max={height_small.max():.1f} threshold={threshold_norm:.1f}")
    

    # Remove tiny specks
    land_mask = morphology.remove_small_objects(land_mask, min_size=20)

    # Connected components
    labeled = measure.label(land_mask, connectivity=2)
    regions = measure.regionprops(labeled)

    # Filter by minimum size
    valid_labels = [r.label for r in regions if r.area >= min_island_pixels]
    sizes = {r.label: r.area for r in regions if r.area >= min_island_pixels}

    # Zero out tiny blobs
    filtered = np.zeros_like(labeled)
    for lbl in valid_labels:
        filtered[labeled == lbl] = lbl

    # Re-label sequentially
    final_labeled = measure.label(filtered > 0, connectivity=2)
    final_regions = measure.regionprops(final_labeled)
    final_sizes = {r.label: r.area for r in final_regions}

    print(f"  Detected {len(final_regions)} landmasses "
          f"(>{min_island_pixels}px at display resolution)")

    return land_mask, final_labeled, len(final_regions), final_sizes


# ---------------------------------------------------------------------------
# GUI — Landmass Labeler
# ---------------------------------------------------------------------------

class LandmassLabeler:
    """
    Simple matplotlib GUI. Shows false-color landmass map.
    Click a landmass → assign continent label from dropdown.
    """

    def __init__(self, height_small, labeled, num_components, sizes, sea_threshold, dtype_max):
        self.height_small = height_small
        self.labeled = labeled
        self.num_components = num_components
        self.sizes = sizes
        self.sea_threshold = sea_threshold
        self.dtype_max = dtype_max
        self.assignments = {}  # label → continent name
        self.selected_label = None

        self.continent_names = list(CONTINENT_OPTIONS.keys())
        self.current_continent_idx = 0

        self._build_gui()

    def _build_gui(self):
        self.fig = plt.figure(figsize=(16, 10))
        self.fig.patch.set_facecolor("#1a1a2e")

        # Main map axis
        self.ax_map = self.fig.add_axes([0.02, 0.15, 0.65, 0.80])
        self.ax_map.set_facecolor("#0a0a1a")
        self.ax_map.set_title(
            "Click a landmass to select it, then assign a continent label",
            color="white", fontsize=11
        )
        self.ax_map.tick_params(colors="white")

        # Draw initial map
        self._draw_map()

        # Info panel
        self.ax_info = self.fig.add_axes([0.69, 0.55, 0.29, 0.40])
        self.ax_info.set_facecolor("#16213e")
        self.ax_info.axis("off")
        self.info_text = self.ax_info.text(
            0.05, 0.95, "Click a landmass\nto begin labeling",
            transform=self.ax_info.transAxes,
            color="white", fontsize=9, va="top", wrap=True
        )

        # Continent selector buttons
        btn_height = 0.048
        btn_gap = 0.008
        start_y = 0.50
        self.continent_buttons = []
        self.continent_axes = []

        for i, name in enumerate(self.continent_names):
            y = start_y - i * (btn_height + btn_gap)
            ax_btn = self.fig.add_axes([0.69, y, 0.29, btn_height])
            btn = Button(ax_btn, name, color="#2d4a7a", hovercolor="#4a7abf")
            btn.label.set_fontsize(8)
            btn.label.set_color("white")
            btn.on_clicked(self._make_assign_callback(name))
            self.continent_buttons.append(btn)
            self.continent_axes.append(ax_btn)

        # Done button
        ax_done = self.fig.add_axes([0.69, 0.03, 0.29, 0.055])
        self.btn_done = Button(ax_done, "✓ DONE — Generate Override Mask",
                               color="#1a6b3a", hovercolor="#2a9b5a")
        self.btn_done.label.set_fontsize(9)
        self.btn_done.label.set_color("white")
        self.btn_done.on_clicked(self._on_done)
        self.done = False

        # Legend
        self.ax_legend = self.fig.add_axes([0.02, 0.02, 0.65, 0.10])
        self.ax_legend.set_facecolor("#1a1a2e")
        self.ax_legend.axis("off")
        self._update_legend()

        self.fig.canvas.mpl_connect("button_press_event", self._on_click)
        plt.show()

    def _draw_map(self):
        self.ax_map.clear()

        # Base: height as grayscale
        display = self.height_small / self.dtype_max
        rgb = np.stack([display, display, display], axis=-1)
        rgb = np.clip(rgb, 0, 1)

        # Color each labeled region
        colors = plt.cm.tab20(np.linspace(0, 1, max(self.num_components, 1)))
        for lbl in range(1, self.labeled.max() + 1):
            mask = self.labeled == lbl
            if not mask.any():
                continue
            color = colors[(lbl - 1) % len(colors)]
            assigned = self.assignments.get(lbl)
            if assigned and assigned != "Skip (ocean/ignore)":
                # Show assigned color
                cont_color = plt.cm.Set1(
                    self.continent_names.index(assigned) / len(self.continent_names)
                )
                rgb[mask] = cont_color[:3]
            else:
                rgb[mask] = color[:3] * 0.6 + 0.1

        # Highlight selected
        if self.selected_label is not None:
            mask = self.labeled == self.selected_label
            rgb[mask] = [1.0, 1.0, 0.0]  # yellow highlight

        self.ax_map.imshow(rgb, origin="upper")
        self.ax_map.set_title(
            f"Landmass Labeler — {len(self.assignments)}/{self.labeled.max()} labeled  "
            f"| Click landmass → assign continent",
            color="white", fontsize=10
        )
        self.ax_map.axis("off")
        self.fig.canvas.draw_idle()

    def _update_legend(self):
        self.ax_legend.clear()
        self.ax_legend.set_facecolor("#1a1a2e")
        self.ax_legend.axis("off")
        patches = []
        assigned_counts = {}
        for lbl, name in self.assignments.items():
            assigned_counts[name] = assigned_counts.get(name, 0) + 1

        for i, (name, info) in enumerate(CONTINENT_OPTIONS.items()):
            count = assigned_counts.get(name, 0)
            color = plt.cm.Set1(i / len(CONTINENT_OPTIONS))
            label_str = f"{name} (val={info['base']}) — {count} blob(s)"
            patches.append(mpatches.Patch(color=color, label=label_str))

        self.ax_legend.legend(
            handles=patches, loc="center", ncol=3, fontsize=7,
            facecolor="#1a1a2e", labelcolor="white", framealpha=0.5
        )
        self.fig.canvas.draw_idle()

    def _on_click(self, event):
        if event.inaxes != self.ax_map:
            return
        if event.xdata is None or event.ydata is None:
            return

        x, y = int(event.xdata), int(event.ydata)
        if x < 0 or y < 0 or x >= self.labeled.shape[1] or y >= self.labeled.shape[0]:
            return

        lbl = self.labeled[y, x]
        if lbl == 0:
            self.info_text.set_text("Clicked ocean/background.\nClick a landmass.")
            self.selected_label = None
        else:
            self.selected_label = lbl
            size = self.sizes.get(lbl, 0)
            current = self.assignments.get(lbl, "unassigned")
            self.info_text.set_text(
                f"Selected: blob #{lbl}\n"
                f"Size: {size:,} px\n"
                f"Current: {current}\n\n"
                f"Click a continent\nbutton to assign →"
            )

        self._draw_map()
        self.fig.canvas.draw_idle()

    def _make_assign_callback(self, continent_name):
        def callback(event):
            if self.selected_label is None:
                self.info_text.set_text("Select a landmass\nfirst by clicking it.")
                self.fig.canvas.draw_idle()
                return
            self.assignments[self.selected_label] = continent_name
            size = self.sizes.get(self.selected_label, 0)
            self.info_text.set_text(
                f"Assigned blob #{self.selected_label}\n"
                f"→ {continent_name}\n"
                f"(size: {size:,} px)\n\n"
                f"Click another landmass\nor click Done."
            )
            self._draw_map()
            self._update_legend()
        return callback

    def _on_done(self, event):
        unassigned = [
            lbl for lbl in range(1, self.labeled.max() + 1)
            if lbl not in self.assignments
        ]
        if unassigned:
            self.info_text.set_text(
                f"⚠ {len(unassigned)} blobs\nnot yet labeled:\n"
                f"{unassigned[:10]}\n\nLabel all or\nmark as Skip."
            )
            self.fig.canvas.draw_idle()
            return
        self.done = True
        plt.close(self.fig)

    def get_assignments(self):
        return self.assignments


# ---------------------------------------------------------------------------
# Override mask generation
# ---------------------------------------------------------------------------

def build_override_mask(
    height_path: Path,
    labeled_small: np.ndarray,
    assignments: dict,
    scale: float,
    orig_size: tuple,
    step0: dict,
    output_size: int = 8192,
) -> np.ndarray:
    """
    Build full-resolution 8-bit override mask.
    Upscales labeled assignments, paints continental base values,
    then applies altitude zone overrides.
    """
    orig_w, orig_h = orig_size
    out_w = min(orig_w, output_size)
    out_h = min(orig_h, output_size)

    print(f"\nBuilding override mask at {out_w}×{out_h}...")
    override = np.zeros((out_h, out_w), dtype=np.uint8)

    # Build label→value lookup
    label_to_value = {}
    for lbl, continent_name in assignments.items():
        value = CONTINENT_OPTIONS[continent_name]["base"]
        label_to_value[lbl] = value

    # Upscale labeled map to output resolution using nearest neighbour
    print("  Upscaling landmass labels...")
    from PIL import Image
    labeled_img = Image.fromarray(labeled_small.astype(np.int32), mode="I")
    labeled_up = labeled_img.resize((out_w, out_h), Image.NEAREST)
    labeled_full = np.array(labeled_up)

    # Paint continental base values
    print("  Painting continental base values...")
    for lbl, value in label_to_value.items():
        mask = labeled_full == lbl
        override[mask] = value

    # Load height at output resolution for altitude zones
    print("  Loading height for altitude zone painting...")
    spline = step0["height_remap_spline"]
    lut_gaea = np.array(spline["lut_256"]["gaea_values"])
    lut_mcy  = np.array(spline["lut_256"]["mc_y_values"])

    with rasterio.open(height_path) as src:
        height_data = src.read(
            1,
            out_shape=(out_h, out_w),
            resampling=rasterio.enums.Resampling.average
        ).astype(np.float32)

    # Convert height to MC Y using LUT
    print("  Applying altitude zone overrides...")
    mc_y = np.interp(height_data, lut_gaea, lut_mcy).astype(np.int32)

    # Apply altitude zones (highest priority last — tundra overwrites alpine)
    land_mask = override > 0
    for zone in ALTITUDE_ZONES:
        zone_mask = land_mask & (mc_y >= zone["mc_y_min"]) & (mc_y < zone["mc_y_max"])
        override[zone_mask] = zone["value"]
        count = zone_mask.sum()
        print(f"    {zone['name']} (val={zone['value']}): {count:,} pixels")

    return override


def build_coastal_reference(
    override: np.ndarray,
    coastal_width_px: int = 80,
) -> np.ndarray:
    """
    Generate coastal proximity reference mask.
    White = within coastal_width_px of land/ocean boundary.
    """
    print("\nBuilding coastal reference mask...")
    land_mask = (override > 0).astype(np.uint8)

    # Distance transform from ocean
    dist_from_ocean = ndimage.distance_transform_edt(land_mask)

    # Coastal zone: land pixels within coastal_width_px of shore
    coastal = (dist_from_ocean > 0) & (dist_from_ocean <= coastal_width_px)
    result = (coastal * 255).astype(np.uint8)
    print(f"  Coastal pixels: {coastal.sum():,}")
    return result


def save_labeled_preview(
    height_small: np.ndarray,
    labeled: np.ndarray,
    assignments: dict,
    dtype_max: float,
    out_path: str,
) -> None:
    """Save false-color labeled landmass preview PNG."""
    display = height_small / dtype_max
    rgb = np.stack([display, display, display], axis=-1)
    rgb = np.clip(rgb, 0, 1) * 0.4  # darken base

    continent_names = list(CONTINENT_OPTIONS.keys())
    for lbl, continent_name in assignments.items():
        mask = labeled == lbl
        idx = continent_names.index(continent_name)
        color = plt.cm.Set1(idx / len(continent_names))
        rgb[mask] = np.array(color[:3]) * 0.8 + 0.1

    plt.imsave(out_path, rgb)
    print(f"  Labeled preview saved → {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Vandir automated override mask painter. Run after Step 0."
    )
    parser.add_argument("--height", required=True,
                        help="Path to height TIFF (Erosion2_Out.tif)")
    parser.add_argument("--step0", default="step0_output.json",
                        help="Path to step0_output.json")
    parser.add_argument("--out-override", default="override_base.png",
                        help="Output override mask PNG")
    parser.add_argument("--out-coastal", default="coastal_reference.png",
                        help="Output coastal reference PNG")
    parser.add_argument("--out-preview", default="landmass_labeled.png",
                        help="Output labeled preview PNG")
    parser.add_argument("--min-island-px", type=int, default=50,
                        help="Min pixel size for landmass detection (display res)")
    parser.add_argument("--coastal-width", type=int, default=80,
                        help="Coastal reference width in output pixels")
    args = parser.parse_args()

    height_path = Path(args.height)
    step0_path  = Path(args.step0)

    if not height_path.exists():
        print(f"ERROR: height file not found: {height_path}", file=sys.stderr)
        sys.exit(1)
    if not step0_path.exists():
        print(f"ERROR: step0_output.json not found: {step0_path}", file=sys.stderr)
        sys.exit(1)

    print("=" * 60)
    print("  Vandir Override Mask Painter")
    print("=" * 60)

    # Load Step 0 results
    print("\nLoading Step 0 results...")
    step0 = load_step0(step0_path)
    sea_threshold = step0["sea_level_threshold_16bit"]
    print(f"  Sea level threshold: {sea_threshold}")

    # Load height downsampled
    print("\nLoading height mask...")
    height_small, scale, orig_size, dtype_max = load_height_downsampled(height_path)

    # Detect landmasses
    print("\nDetecting landmasses...")
    land_mask, labeled, num_components, sizes = detect_landmasses(
        height_small, sea_threshold, dtype_max,
        min_island_pixels=args.min_island_px
    )
    print(f"  Found {num_components} landmasses to label")

    # Launch GUI
    print("\nLaunching labeling GUI...")
    print("  → Click each landmass, assign a continent, click Done when finished.")
    labeler = LandmassLabeler(
        height_small, labeled, num_components, sizes, sea_threshold, dtype_max
    )
    assignments = labeler.get_assignments()

    if not assignments:
        print("No assignments made. Exiting.")
        sys.exit(0)

    print(f"\nAssignments confirmed: {len(assignments)} landmasses labeled")
    for lbl, name in assignments.items():
        print(f"  Blob #{lbl} → {name} (value={CONTINENT_OPTIONS[name]['base']})")

    # Save labeled preview
    print("\nSaving labeled preview...")
    save_labeled_preview(height_small, labeled, assignments, dtype_max, args.out_preview)

    # Build override mask
    override = build_override_mask(
        height_path, labeled, assignments, scale, orig_size, step0
    )

    # Build coastal reference
    coastal = build_coastal_reference(override, coastal_width_px=args.coastal_width)

    # Save outputs
    print("\nSaving outputs...")
    from PIL import Image
    Image.fromarray(override).save(args.out_override)
    print(f"  Override mask saved → {args.out_override}")

    Image.fromarray(coastal).save(args.out_coastal)
    print(f"  Coastal reference saved → {args.out_coastal}")

    print("\n" + "=" * 60)
    print("  OVERRIDE PAINTING COMPLETE")
    print(f"  Override mask : {args.out_override}")
    print(f"  Coastal ref   : {args.out_coastal}")
    print(f"  Preview       : {args.out_preview}")
    print("\n  Next steps:")
    print("  1. Open coastal_reference.png alongside override_base.png in Photoshop")
    print("  2. Paint coastal strip biomes manually using coastal_reference as guide")
    print("  3. Paint river valleys, rain shadows, and detail zones")
    print("  4. Save final as override_final.png (8-bit grayscale)")
    print("=" * 60)


if __name__ == "__main__":
    main()
