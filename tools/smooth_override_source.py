"""
smooth_override_source.py
Applies size-aware Gaussian boundary smoothing to override_final.png.

Large continental zones  → sigma=30  (eliminates staircase artifacts)
Thin/narrow zones        → sigma=3   (protects rivers, coastal strips)

"Thinness" is estimated via area/perimeter ratio (= half the mean width).
Zones with estimated mean width < WIDTH_THRESHOLD pixels are treated as thin.

After smoothing, thin zones are stamped back at their original boundaries
so sigma=30 bleed from large zones cannot erase them.

Writes the result back to override_final.png (original backed up first),
then regenerates masks/override.tif via rasterio nearest-neighbour upscale.
"""
import shutil
import numpy as np
from pathlib import Path
from PIL import Image
from scipy.ndimage import gaussian_filter, label

# ── Config ────────────────────────────────────────────────────────────────────
SRC_PNG   = Path(r"C:\Users\nicho\minecraft-worldgen\override_final.png")
BACKUP    = Path(r"C:\Users\nicho\minecraft-worldgen\override_final_backup.png")
TIF_OUT   = Path(r"C:\Users\nicho\minecraft-worldgen\masks\override.tif")

SIGMA_BIG  = 60    # applied to wide continental zones
SIGMA_THIN = 3     # applied to rivers/coastal strips
WIDTH_THRESHOLD = 80   # px in 8192-space; zones below this are "thin"
# Zones to force-protect regardless of width (e.g. elongated but regionally important)
FORCE_THIN = {200}  # SEMI_ARID_SHRUBLAND — borderline width, important region shape
# ─────────────────────────────────────────────────────────────────────────────


def estimate_mean_width(mask: np.ndarray) -> float:
    """
    Estimate mean width of a binary zone mask via area / perimeter × 2.
    For a thin rectangle of width W and length L >> W:
      area ≈ W*L,  perimeter ≈ 2*L  →  mean_width ≈ area/perimeter*2 = W
    Returns float (pixels in source image space).
    """
    area = int(mask.sum())
    if area == 0:
        return 0.0
    # Perimeter: pixels where mask differs from a 1-pixel shift
    shifted_r = np.roll(mask, 1, axis=0)
    shifted_c = np.roll(mask, 1, axis=1)
    perim = int(((mask != shifted_r) | (mask != shifted_c)).sum())
    if perim == 0:
        return float(area)
    return 2.0 * area / perim


def main():
    # ── Load ─────────────────────────────────────────────────────────────────
    # Always read from the original backup so repeated runs don't compound blurs.
    src = BACKUP if BACKUP.exists() else SRC_PNG
    if src == BACKUP:
        print(f"Reading from backup (original): {BACKUP}")
    else:
        shutil.copy2(SRC_PNG, BACKUP)
        print(f"Backed up original to {BACKUP}")

    img = Image.open(src).convert("L")
    arr = np.array(img, dtype=np.uint8)
    H, W = arr.shape
    print(f"Size: {W}x{H}")

    zones = sorted(z for z in np.unique(arr).tolist() if z != 0)
    print(f"Zones ({len(zones)}): {zones}\n")

    # ── Classify zones ────────────────────────────────────────────────────────
    thin_zones = []
    big_zones  = []
    for z in zones:
        mask = arr == z
        mw = estimate_mean_width(mask)
        is_thin = (mw < WIDTH_THRESHOLD) or (z in FORCE_THIN)
        tag = "thin" if is_thin else "BIG "
        if z in FORCE_THIN and mw >= WIDTH_THRESHOLD:
            tag += " [FORCE_THIN]"
        print(f"  zone {z:3d}  area={mask.sum():>8,}  mean_width={mw:6.1f}px  → {tag}")
        (thin_zones if is_thin else big_zones).append(z)

    print(f"\nBIG  zones ({len(big_zones)}):  sigma={SIGMA_BIG}  → {big_zones}")
    print(f"thin zones ({len(thin_zones)}):  sigma={SIGMA_THIN} → {thin_zones}\n")

    # ── Boundary pixels before ────────────────────────────────────────────────
    sr = np.roll(arr, 1, axis=0); sc = np.roll(arr, 1, axis=1)
    before = int(((arr != sr) | (arr != sc)).sum())
    print(f"Boundary pixels before: {before:,}")

    # ── Build per-zone probability fields with per-zone sigma ─────────────────
    prob_stack = np.zeros((len(zones), H, W), dtype=np.float32)
    for i, z in enumerate(zones):
        sigma = SIGMA_THIN if z in thin_zones else SIGMA_BIG
        prob_stack[i] = gaussian_filter((arr == z).astype(np.float32), sigma=sigma)
        print(f"  blurred zone {z:3d}  sigma={sigma}  ({i+1}/{len(zones)})")

    # ── Argmax over non-zero pixels ───────────────────────────────────────────
    result = np.zeros_like(arr)
    has_zone = arr != 0
    best = np.argmax(prob_stack, axis=0)
    for i, z in enumerate(zones):
        result[has_zone & (best == i)] = z

    # ── Stamp thin zones back at their ORIGINAL pixels ────────────────────────
    # Prevents sigma=30 bleed from big zones erasing rivers / coastal strips.
    for z in thin_zones:
        original_mask = arr == z
        result[original_mask] = z
    print(f"\nStamped {len(thin_zones)} thin zones back at original pixels.")

    # ── Boundary pixels after ─────────────────────────────────────────────────
    rr = np.roll(result, 1, axis=0); rc = np.roll(result, 1, axis=1)
    after = int(((result != rr) | (result != rc)).sum())
    print(f"Boundary pixels after:  {after:,}  "
          f"({100*(before-after)/max(before,1):.1f}% reduction)")

    # ── Write new override_final.png ──────────────────────────────────────────
    Image.fromarray(result, mode="L").save(str(SRC_PNG))
    print(f"\nWritten smoothed image to {SRC_PNG}")

    # ── Regenerate override.tif ───────────────────────────────────────────────
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds

    TARGET = 50000
    print(f"Upscaling to {TIF_OUT}  ({TARGET}x{TARGET}) ...")
    with rasterio.open(str(SRC_PNG)) as src:
        profile = src.profile.copy()
        profile.update(
            width=TARGET, height=TARGET,
            driver="GTiff", bigtiff="YES",
            tiled=True, blockxsize=512, blockysize=512,
            compress="deflate",
            transform=from_bounds(0, 0, 1, 1, TARGET, TARGET),
        )
        data = src.read(1, out_shape=(TARGET, TARGET),
                        resampling=Resampling.nearest)
    with rasterio.open(str(TIF_OUT), "w", **profile) as dst:
        dst.write(data, 1)
    print(f"Done.  {TIF_OUT}  ({TIF_OUT.stat().st_size/1024/1024:.1f} MB)")


if __name__ == "__main__":
    main()
