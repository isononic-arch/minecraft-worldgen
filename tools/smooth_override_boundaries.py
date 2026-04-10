"""
smooth_override_boundaries.py
Applies morphological smoothing to override_final.png to eliminate staircase
artifacts on diagonal zone boundaries.

Gaussian blur cannot fix a perfectly horizontal/vertical staircase pattern;
it only softens it slightly. Morphological closing+opening with a CIRCULAR
structuring element actually reconstructs smooth diagonal boundaries because
the disk SE cannot fit into the right-angle corners of a staircase — so it
rounds them off into circular arcs.

Algorithm per zone:
  1. Binary mask for zone
  2. binary_closing  (fills convex corners / connects gaps)  with disk radius R
  3. binary_opening  (removes convex protrusions / staircase teeth) with disk R
  4. Winner-takes-all argmax over all smoothed masks
  5. Stamp thin/protected zones back at their ORIGINAL pixels

Large zones  → R=RADIUS_BIG  (e.g. 15 source pixels)
Thin zones   → R=RADIUS_THIN (e.g. 3  source pixels, preserves rivers)

Reads from override_final_backup.png (original) to avoid compounding edits.
Writes result back to override_final.png and regenerates masks/override.tif.
Also saves a diagnostic crop PNG showing before/after.
"""
import shutil
import numpy as np
from pathlib import Path
from PIL import Image
from scipy.ndimage import binary_opening, distance_transform_edt

# ── Config ────────────────────────────────────────────────────────────────────
SRC_PNG   = Path(r"C:\Users\nicho\minecraft-worldgen\override_final.png")
BACKUP    = Path(r"C:\Users\nicho\minecraft-worldgen\override_final_backup.png")
TIF_OUT   = Path(r"C:\Users\nicho\minecraft-worldgen\masks\override.tif")
CROP_OUT  = Path(r"C:\Users\nicho\minecraft-worldgen\tools\boundary_crop_compare.png")

RADIUS_BIG  = 20   # pixels in 8192-space for large continental zones
RADIUS_THIN = 3    # pixels for thin zones (rivers, coastal strips)
WIDTH_THRESHOLD = 50   # zones below this mean-width (px) are "thin"
FORCE_THIN = {200}     # SEMI_ARID_SHRUBLAND — protect regardless of width
# ─────────────────────────────────────────────────────────────────────────────


def disk(radius: int) -> np.ndarray:
    """Return a boolean circular structuring element of given radius."""
    r = int(radius)
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return (x * x + y * y) <= r * r


def estimate_mean_width(mask: np.ndarray) -> float:
    """Estimate mean width via 2 × area / perimeter."""
    area = int(mask.sum())
    if area == 0:
        return 0.0
    shifted_r = np.roll(mask, 1, axis=0)
    shifted_c = np.roll(mask, 1, axis=1)
    perim = int(((mask != shifted_r) | (mask != shifted_c)).sum())
    if perim == 0:
        return float(area)
    return 2.0 * area / perim


def save_crop(before: np.ndarray, after: np.ndarray, path: Path):
    """Find the boundary-densest 512x512 region and save a side-by-side crop."""
    H, W = before.shape
    best_score = -1
    bx, by = 0, 0
    step = 128
    cw, ch = 512, 512
    for y in range(0, H - ch, step):
        for x in range(0, W - cw, step):
            patch = before[y:y + ch, x:x + cw]
            sr = np.roll(patch, 1, axis=0)
            sc = np.roll(patch, 1, axis=1)
            score = int(((patch != sr) | (patch != sc)).sum())
            if score > best_score:
                best_score = score
                by, bx = y, x

    b_patch = before[by:by + ch, bx:bx + cw]
    a_patch = after [by:by + ch, bx:bx + cw]

    def to_img(arr):
        vals = np.unique(arr)
        lut = {v: int(i * 255 / max(len(vals) - 1, 1)) for i, v in enumerate(vals)}
        out = np.vectorize(lut.get)(arr).astype(np.uint8)
        return Image.fromarray(out, mode="L")

    bi = to_img(b_patch)
    ai = to_img(a_patch)
    combined = Image.new("L", (cw * 2 + 8, ch), color=128)
    combined.paste(bi, (0, 0))
    combined.paste(ai, (cw + 8, 0))
    combined.save(str(path))
    print(f"Saved boundary crop to {path}  (region x={bx} y={by}, score={best_score:,})")


def main():
    # ── Load ─────────────────────────────────────────────────────────────────
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
        print(f"  zone {z:3d}  area={mask.sum():>8,}  mean_width={mw:6.1f}px  -> {tag}")
        (thin_zones if is_thin else big_zones).append(z)

    print(f"\nBIG  zones ({len(big_zones)}):  R={RADIUS_BIG}  -> {big_zones}")
    print(f"thin zones ({len(thin_zones)}):  R={RADIUS_THIN} -> {thin_zones}\n")

    # ── Boundary pixels before ────────────────────────────────────────────────
    sr = np.roll(arr, 1, axis=0)
    sc = np.roll(arr, 1, axis=1)
    before_count = int(((arr != sr) | (arr != sc)).sum())
    print(f"Boundary pixels before: {before_count:,}")

    # ── Voronoi expansion from morphologically-smoothed cores ─────────────────
    # Strategy:
    #   1. binary_opening with circular disk removes staircase teeth from each zone
    #      (the disk cannot fit into right-angle staircase corners → they get rounded off)
    #   2. distance_transform_edt gives distance from each pixel to the zone's smooth core
    #   3. Winner = zone whose core is CLOSEST → Voronoi boundary between smooth cores
    #      → inherently smooth curve, no staircase
    #
    # Score = -distance_to_core  (0 inside core, negative outside)
    # Zones processed in order; ties broken by first-seen (fine in practice).
    struct_big  = disk(RADIUS_BIG)
    struct_thin = disk(RADIUS_THIN)

    has_zone  = arr != 0
    best_val  = np.full((H, W), -np.inf, dtype=np.float32)
    best_zone = np.zeros((H, W), dtype=np.uint8)

    for i, z in enumerate(zones):
        mask = (arr == z)
        struct = struct_thin if z in thin_zones else struct_big
        core = binary_opening(mask, structure=struct)
        # EDT: 0 inside core, positive distance outside
        dist  = distance_transform_edt(~core).astype(np.float32)
        score = -dist   # 0 at core, decreasing with distance
        r = RADIUS_THIN if z in thin_zones else RADIUS_BIG
        print(f"  zone {z:3d}  R={r}  core={int(core.sum()):>7,}px  ({i+1}/{len(zones)})")

        wins = has_zone & (score > best_val)
        best_val[wins]  = score[wins]
        best_zone[wins] = z

    result = best_zone

    # ── Stamp thin zones back at their ORIGINAL pixels ────────────────────────
    for z in thin_zones:
        result[arr == z] = z
    print(f"\nStamped {len(thin_zones)} thin zones back at original pixels.")

    # ── Boundary pixels after ─────────────────────────────────────────────────
    rr = np.roll(result, 1, axis=0)
    rc = np.roll(result, 1, axis=1)
    after_count = int(((result != rr) | (result != rc)).sum())
    print(f"Boundary pixels after:  {after_count:,}  "
          f"({100*(before_count - after_count)/max(before_count, 1):.1f}% reduction)")

    # ── Diagnostic crop ───────────────────────────────────────────────────────
    save_crop(arr, result, CROP_OUT)

    # ── Write new override_final.png ──────────────────────────────────────────
    Image.fromarray(result, mode="L").save(str(SRC_PNG))
    print(f"\nWritten smoothed image to {SRC_PNG}")

    # ── Regenerate override.tif ───────────────────────────────────────────────
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds

    TARGET = 50000
    print(f"Upscaling to {TIF_OUT}  ({TARGET}x{TARGET}) ...")
    with rasterio.open(str(SRC_PNG)) as src_r:
        profile = src_r.profile.copy()
        profile.update(
            width=TARGET, height=TARGET,
            driver="GTiff", bigtiff="YES",
            tiled=True, blockxsize=512, blockysize=512,
            compress="deflate",
            transform=from_bounds(0, 0, 1, 1, TARGET, TARGET),
        )
        data = src_r.read(1, out_shape=(TARGET, TARGET),
                          resampling=Resampling.nearest)
    with rasterio.open(str(TIF_OUT), "w", **profile) as dst:
        dst.write(data, 1)
    print(f"Done.  {TIF_OUT}  ({TIF_OUT.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
