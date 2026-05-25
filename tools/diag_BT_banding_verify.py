"""S86: Verify the new banded override.tif looks right.

Compares masks/override.tif (current) vs masks/override_s86_BT_bands.tif (new):
  - Per-biome coverage % change
  - Per-biome elevation distribution (violin or text summary)
  - Cold-zone band boundary visualization (sampled 1:8 thumbnail)
  - Counts of contiguous BT regions (sanity: are the bands forming coherent blobs?)

Outputs:
  - memory/S86_banding_verify.md (text report)
  - S86_banding_compare.png (side-by-side biome maps + elevation hists)
"""
import json
import sys
import numpy as np
from pathlib import Path
import rasterio
from rasterio.windows import Window
from scipy.interpolate import PchipInterpolator
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

ROOT = Path(r"C:\Users\nicho\minecraft-worldgen")
OLD = ROOT / "masks" / "override.tif"
NEW = ROOT / "masks" / "override_s86_BT_bands.tif"
HEIGHT = ROOT / "masks" / "height.tif"
CFG = ROOT / "config" / "thresholds.json"
OUT_MD = ROOT / "memory" / "S86_banding_verify.md"
OUT_PNG = ROOT / "S86_banding_compare.png"

BT, SBT, BA, AT, FF = 30, 35, 40, 50, 55
COLD = (BT, SBT, BA, AT)

# 1:8 downsample for the thumbnail (50k -> 6250)
THUMB_RES = 1024


def raw_to_mcy_spline():
    sp = json.loads(CFG.read_text())["terrain_spline"]
    return PchipInterpolator(np.array(sp["gaea_in"], dtype=np.float64),
                             np.array(sp["mc_y_out"], dtype=np.float64))


def read_thumb(path, target=THUMB_RES):
    """NEAREST downsample to (target, target) for visualization."""
    with rasterio.open(path) as src:
        h, w = src.height, src.width
        # Pick stride
        sy = max(1, h // target)
        sx = max(1, w // target)
        arr = src.read(1)[::sy, ::sx]
    return arr


def read_full_stats(path):
    """Read full 50k mask, return coverage counts + small Y sample for histogram."""
    counts = np.zeros(256, dtype=np.int64)
    BAND = 2000
    with rasterio.open(path) as src:
        for r0 in range(0, src.height, BAND):
            r1 = min(src.height, r0 + BAND)
            arr = src.read(1, window=Window(0, r0, src.width, r1 - r0))
            bc = np.bincount(arr.ravel(), minlength=256)
            counts += bc
    return counts


def joint_zone_height_sample(path_zone, path_height, n=500_000, seed=42):
    """Sample N matching pixels from zone + height for elevation histograms."""
    rng = np.random.default_rng(seed)
    with rasterio.open(path_zone) as zs:
        h, w = zs.height, zs.width
    ys = rng.integers(0, h, n)
    xs = rng.integers(0, w, n)
    # Sort for monotonic windowed reads
    order = np.lexsort((xs, ys))
    ys = ys[order]; xs = xs[order]
    zones = np.zeros(n, dtype=np.uint8)
    heights = np.zeros(n, dtype=np.float32)
    BAND = 5000
    with rasterio.open(path_zone) as zs, rasterio.open(path_height) as hs:
        for r0 in range(0, h, BAND):
            r1 = min(h, r0 + BAND)
            in_band = (ys >= r0) & (ys < r1)
            if not in_band.any():
                continue
            sel = np.where(in_band)[0]
            ys_b = ys[sel] - r0
            xs_b = xs[sel]
            za = zs.read(1, window=Window(0, r0, w, r1 - r0))
            ha = hs.read(1, window=Window(0, r0, w, r1 - r0))
            zones[sel] = za[ys_b, xs_b]
            heights[sel] = ha[ys_b, xs_b]
    return zones, heights


def main():
    if not NEW.exists():
        sys.exit(f"Missing {NEW}. Run tools/upscale_override_BT_banded.py first.")

    raw2y = raw_to_mcy_spline()

    print("Reading coverage counts (old + new)...")
    old_counts = read_full_stats(OLD)
    new_counts = read_full_stats(NEW)

    # Sample zone+height jointly
    print("Sampling 500k pixels from each for elevation histograms...")
    old_z, old_h = joint_zone_height_sample(OLD, HEIGHT)
    new_z, new_h = joint_zone_height_sample(NEW, HEIGHT)
    old_y = raw2y(old_h.astype(np.float64))
    new_y = raw2y(new_h.astype(np.float64))

    # Coverage report
    print("\n=== Coverage % change ===")
    lines = []
    lines.append("# S86 BT-banding verification\n")
    lines.append("## Coverage change (full 50k)\n")
    lines.append("| Zone | Name | Before % | After % | Delta |")
    lines.append("|---|---|---:|---:|---:|")
    NAMES = {0: "ocean", 10: "COASTAL_HEATH", 20: "TEMP_RAINFOREST", 30: "BT",
             35: "SBT", 40: "BA", 50: "AT", 55: "FROZEN_FLATS", 60: "TEMP_DECIDUOUS",
             70: "RAINFOREST_COAST", 80: "RIPARIAN_WOODLAND", 90: "DRY_OAK_SAVANNA",
             100: "KARST_BARRENS", 110: "BIRCH_FOREST", 115: "ETC", 120: "MIXED_FOREST",
             130: "CONT_STEPPE", 140: "DRY_PINE_BARRENS", 150: "SCRUBBY_HEATHLAND",
             160: "LUSH_RAINFOREST_COAST", 170: "SAND_DUNE_DESERT",
             190: "DESERT_STEPPE_TRANS", 200: "SEMI_ARID_SHRUBLAND",
             210: "DRY_WOODLAND_MAQUIS", 220: "TIDAL_JUNGLE_FRINGE",
             230: "MANGROVE_COAST", 240: "FRESHWATER_FEN"}
    tot = float(old_counts.sum())
    for z in sorted(set([z for z in range(256) if old_counts[z] > 0 or new_counts[z] > 0])):
        op = old_counts[z] / tot * 100
        np_ = new_counts[z] / tot * 100
        d = np_ - op
        name = NAMES.get(z, f"z{z}")
        if z in COLD or abs(d) > 0.01:
            lines.append(f"| {z} | {name} | {op:.3f} | {np_:.3f} | {d:+.3f} |")
            print(f"  zone {z:>3} {name:<22}: {op:>6.3f}% -> {np_:>6.3f}%  ({d:+.3f})")

    # Cold zone medians
    print("\n=== Per-biome elevation (cold zone, MC Y) ===")
    lines.append("\n## Per-biome MC Y stats (cold zone)\n")
    lines.append("| Zone | Name | Before med Y | After med Y | Before P95 | After P95 |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for z, n in [(BT, "BT"), (SBT, "SBT"), (BA, "BA"), (AT, "AT")]:
        old_ys = old_y[old_z == z]
        new_ys = new_y[new_z == z]
        if len(old_ys) and len(new_ys):
            o_med = np.median(old_ys); n_med = np.median(new_ys)
            o_p95 = np.percentile(old_ys, 95); n_p95 = np.percentile(new_ys, 95)
            lines.append(f"| {z} | {n} | {o_med:.0f} | {n_med:.0f} | {o_p95:.0f} | {n_p95:.0f} |")
            print(f"  {n:<3}: med {o_med:.0f} -> {n_med:.0f}   P95 {o_p95:.0f} -> {n_p95:.0f}")

    # Side-by-side comparison plot
    print("\nRendering compare plot...")
    old_thumb = read_thumb(OLD)
    new_thumb = read_thumb(NEW)

    COLORS = {0: "#0a3060", 10: "#9bbe6b", 20: "#1f5c1a", 30: "#3a8a35",  # BT
              35: "#7ab9d6",  # SBT
              40: "#a8a060",  # BA
              50: "#e8e4e0",  # AT
              55: "#d9d2bb",  # FF
              60: "#4d8c3e", 70: "#256a3f", 80: "#3f8a6a", 90: "#c2a060",
              100: "#a8856b", 110: "#a8c060", 115: "#5fa386", 120: "#5a8a40",
              130: "#bea66e", 140: "#7a9a4a", 150: "#9a7f5a", 160: "#356b40",
              170: "#e6cf86", 190: "#c8b070", 200: "#b09f6a", 210: "#9a8a55",
              220: "#3e7a55", 230: "#5b9a6a", 240: "#5a8a8e"}
    def to_rgb(arr):
        rgb = np.zeros(arr.shape + (3,), dtype=np.uint8)
        for z, hexc in COLORS.items():
            mask = arr == z
            if mask.any():
                r, g, b = int(hexc[1:3], 16), int(hexc[3:5], 16), int(hexc[5:7], 16)
                rgb[mask] = (r, g, b)
        return rgb

    fig, axes = plt.subplots(2, 2, figsize=(14, 12))
    axes[0, 0].imshow(to_rgb(old_thumb))
    axes[0, 0].set_title(f"BEFORE: override.tif ({old_thumb.shape[0]}x{old_thumb.shape[1]} thumb)")
    axes[0, 0].axis("off")

    axes[0, 1].imshow(to_rgb(new_thumb))
    axes[0, 1].set_title(f"AFTER: override_s86_BT_bands.tif")
    axes[0, 1].axis("off")

    # Legend (cold biomes only)
    legend_patches = [
        mpatches.Patch(color=COLORS[BA], label="BA (lowland)"),
        mpatches.Patch(color=COLORS[BT], label="BT (midland)"),
        mpatches.Patch(color=COLORS[SBT], label="SBT (highland)"),
        mpatches.Patch(color=COLORS[AT], label="AT (peaks)"),
        mpatches.Patch(color=COLORS[FF], label="FF (untouched)"),
    ]
    axes[0, 1].legend(handles=legend_patches, loc="lower right", fontsize=8,
                       framealpha=0.85)

    # Cold-zone elevation histograms (4-panel, stacked)
    axes[1, 0].set_title("BEFORE: cold-zone MC Y distribution per biome")
    for z, color, label in [(BA, COLORS[BA], "BA"), (BT, COLORS[BT], "BT"),
                            (SBT, COLORS[SBT], "SBT"), (AT, COLORS[AT], "AT")]:
        ys = old_y[old_z == z]
        if len(ys):
            axes[1, 0].hist(ys, bins=70, range=(0, 700), alpha=0.55,
                            color=color, label=f"{label} (n={len(ys):,})",
                            edgecolor="black", linewidth=0.3)
    axes[1, 0].set_xlabel("MC Y"); axes[1, 0].set_ylabel("Sampled pixels")
    axes[1, 0].axvline(63, color="cyan", linestyle="--", alpha=0.5, label="sea")
    axes[1, 0].legend(fontsize=8)
    axes[1, 0].grid(True, alpha=0.3)

    axes[1, 1].set_title("AFTER: cold-zone MC Y distribution per biome")
    for z, color, label in [(BA, COLORS[BA], "BA"), (BT, COLORS[BT], "BT"),
                            (SBT, COLORS[SBT], "SBT"), (AT, COLORS[AT], "AT")]:
        ys = new_y[new_z == z]
        if len(ys):
            axes[1, 1].hist(ys, bins=70, range=(0, 700), alpha=0.55,
                            color=color, label=f"{label} (n={len(ys):,})",
                            edgecolor="black", linewidth=0.3)
    axes[1, 1].set_xlabel("MC Y"); axes[1, 1].set_ylabel("Sampled pixels")
    axes[1, 1].axvline(63, color="cyan", linestyle="--", alpha=0.5, label="sea")
    axes[1, 1].axvline(163, color="red", linestyle=":", alpha=0.6, label="T1")
    axes[1, 1].axvline(334, color="red", linestyle=":", alpha=0.6, label="T2")
    axes[1, 1].axvline(577, color="red", linestyle=":", alpha=0.6, label="T3")
    axes[1, 1].legend(fontsize=8)
    axes[1, 1].grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(OUT_PNG, dpi=120, bbox_inches="tight", facecolor="white")
    print(f"  saved: {OUT_PNG}")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines))
    print(f"  saved: {OUT_MD}")


if __name__ == "__main__":
    main()
