"""S86: Render a fresh cold-biome map from masks/override_s86_BT_bands.tif
to compare against cold_biomes_map.png (S85 reference).

Colors match the user's reference:
  BA  (40) -> olive (warmest cold biome, lowland)
  BT  (30) -> bright green
  SBT (35) -> mid blue
  AT  (50) -> white / pale cyan

Outputs:
  cold_biomes_map_S86.png   (1024x1024 same crop)
  cold_biomes_compare_S86.png  (side-by-side: S85 ref vs S86 new)
"""
import numpy as np
from pathlib import Path
import rasterio
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from PIL import Image

ROOT = Path(r"C:\Users\nicho\minecraft-worldgen")
NEW = ROOT / "masks" / "override_s86_BT_bands.tif"
OLD = ROOT / "masks" / "override.tif"
REF = ROOT / "cold_biomes_map.png"  # S85 reference
OUT_NEW = ROOT / "cold_biomes_map_S86.png"
OUT_CMP = ROOT / "cold_biomes_compare_S86.png"

BT, SBT, BA, AT, FF = 30, 35, 40, 50, 55
COLD = {BT, SBT, BA, AT}
LAND_THRESHOLD = 0  # any zone > 0 is land

# Colors (RGB) - matching the S85 reference visualization
COL_OCEAN = (10, 30, 60)        # dark blue
COL_LAND  = (60, 60, 55)        # dark gray-green
COL_BA    = (140, 130, 70)      # olive
COL_BT    = (50, 180, 60)       # bright green
COL_SBT   = (80, 140, 220)      # mid blue
COL_AT    = (235, 240, 245)     # near-white

THUMB = 1024


def downsample_nearest(path, target=THUMB):
    with rasterio.open(path) as src:
        sh, sw = src.height, src.width
        sy = max(1, sh // target)
        sx = max(1, sw // target)
        return src.read(1)[::sy, ::sx]


def render_cold_map(arr):
    """Map zone codes -> RGB. ocean=zone 0, land=other-non-cold, then BA/BT/SBT/AT."""
    rgb = np.empty(arr.shape + (3,), dtype=np.uint8)
    ocean = (arr == 0)
    rgb[ocean] = COL_OCEAN
    rgb[~ocean] = COL_LAND  # default land
    for z, c in [(BA, COL_BA), (BT, COL_BT), (SBT, COL_SBT), (AT, COL_AT)]:
        rgb[arr == z] = c
    return rgb


def main():
    print(f"Loading new override ({NEW.name})...")
    new = downsample_nearest(NEW)
    print(f"  shape: {new.shape}")

    # Count cold-zone pixels (at thumbnail resolution)
    counts = {z: int((new == z).sum()) for z in (BA, BT, SBT, AT)}
    land = int((new > 0).sum())
    print(f"\nCold-biome coverage (1024x1024 thumb, % of LAND):")
    for z, name in [(BA, "BA"), (BT, "BT"), (SBT, "SBT"), (AT, "AT")]:
        pct = counts[z] / land * 100 if land else 0
        print(f"  {name:>3}: {counts[z]:>8,} px  ({pct:>5.2f}% of land)")

    rgb_new = render_cold_map(new)

    # Save new map alone
    fig, ax = plt.subplots(figsize=(10, 10), facecolor=tuple(c/255 for c in COL_OCEAN))
    ax.imshow(rgb_new)
    ax.set_title(f"COLD BIOMES (S86 banded) — total cold = {sum(counts.values()):,} thumb px")
    ax.axis("off")
    # Legend
    patches = [
        mpatches.Patch(color=tuple(c/255 for c in COL_BA), label=f"BA  ({counts[BA]:>6,} px / {counts[BA]/land*100:.2f}%)"),
        mpatches.Patch(color=tuple(c/255 for c in COL_BT), label=f"BT  ({counts[BT]:>6,} px / {counts[BT]/land*100:.2f}%)"),
        mpatches.Patch(color=tuple(c/255 for c in COL_SBT), label=f"SBT ({counts[SBT]:>6,} px / {counts[SBT]/land*100:.2f}%)"),
        mpatches.Patch(color=tuple(c/255 for c in COL_AT), label=f"AT  ({counts[AT]:>6,} px / {counts[AT]/land*100:.2f}%)"),
    ]
    ax.legend(handles=patches, loc="upper left", fontsize=9, framealpha=0.85)
    plt.tight_layout()
    plt.savefig(OUT_NEW, dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close()
    print(f"\nSaved {OUT_NEW.name}")

    # Side-by-side compare with S85 reference (if present)
    if REF.exists():
        print(f"Loading S85 reference ({REF.name})...")
        ref_img = np.array(Image.open(REF).convert("RGB"))
        # Reference is roughly 1024x1024
        fig, axes = plt.subplots(1, 2, figsize=(20, 11),
                                   facecolor=tuple(c/255 for c in COL_OCEAN))
        axes[0].imshow(ref_img)
        axes[0].set_title("BEFORE: S85 reference (cold_biomes_map.png)", fontsize=14, color="white")
        axes[0].axis("off")
        axes[1].imshow(rgb_new)
        axes[1].set_title("AFTER: S86 banded (cold_biomes_map_S86.png)", fontsize=14, color="white")
        axes[1].axis("off")
        axes[1].legend(handles=patches, loc="upper left", fontsize=9, framealpha=0.85)
        plt.tight_layout()
        plt.savefig(OUT_CMP, dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close()
        print(f"Saved {OUT_CMP.name}")
    else:
        print(f"  (S85 ref {REF} not found; skipping compare)")


if __name__ == "__main__":
    main()
