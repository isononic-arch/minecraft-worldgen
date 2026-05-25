"""S86: Apply BT-banding to cold-zone pixels.

Strategy: read override_vectorized.png (8192 borders) + override_final.png (8192 fill),
build composite, then for cold-zone pixels {BT 30, SBT 35, BA 40, AT 50} reassign by
MC-Y elevation band sampled from height.tif (downsampled to 8192).

Bands (T1=163, T2=334, T3=577 from diag_BT_banding_propose.py):
  Y <  163        -> BA  (40)  lowland
  163 <= Y < 334  -> BT  (30)  midland
  334 <= Y < 577  -> SBT (35)  highland
  Y >= 577        -> AT  (50)  alpine peaks

Output: override_banded_s86.png (8192) — composite with bands applied.
        Re-use this as BOTH input and base in upscale_override_BT_banded.py.

Untouched zones: everything outside {30, 35, 40, 50}. FF (55) explicitly preserved.
"""
import json
import numpy as np
from pathlib import Path
from PIL import Image
import rasterio
from rasterio.windows import Window
from scipy.interpolate import PchipInterpolator

ROOT = Path(r"C:\Users\nicho\minecraft-worldgen")
VEC  = ROOT / "override_vectorized.png"
BASE = ROOT / "override_final.png"
HEIGHT = ROOT / "masks" / "height.tif"
CFG  = ROOT / "config" / "thresholds.json"
OUT  = ROOT / "override_banded_s86.png"

BT, SBT, BA, AT = 30, 35, 40, 50
COLD = (BT, SBT, BA, AT)

# Thresholds from diag_BT_banding_propose.py scheme A (equal-area splits)
T1 = 163  # BA -> BT
T2 = 334  # BT -> SBT
T3 = 577  # SBT -> AT


def raw_to_mcy_spline():
    sp = json.loads(CFG.read_text())["terrain_spline"]
    return PchipInterpolator(np.array(sp["gaea_in"], dtype=np.float64),
                             np.array(sp["mc_y_out"], dtype=np.float64))


def downsample_height(target_h, target_w):
    """Block-mean downsample masks/height.tif (50k) to (target_h, target_w)."""
    h_src = np.zeros((target_h, target_w), dtype=np.float32)
    with rasterio.open(HEIGHT) as src:
        sh, sw = src.height, src.width
        BAND = 1024
        for r0 in range(0, target_h, BAND):
            r1 = min(target_h, r0 + BAND)
            sr0 = int(r0 * sh / target_h)
            sr1 = int(r1 * sh / target_h)
            sh_band = sr1 - sr0
            arr = src.read(1, window=Window(0, sr0, sw, sh_band)).astype(np.float32)
            for r in range(r0, r1):
                src_r_lo = int((r - r0) * sh_band / (r1 - r0))
                src_r_hi = int((r - r0 + 1) * sh_band / (r1 - r0))
                if src_r_hi <= src_r_lo:
                    src_r_hi = src_r_lo + 1
                row_strip = arr[src_r_lo:src_r_hi].mean(axis=0)
                cidx = np.linspace(0, sw, target_w + 1, dtype=np.int64)
                cum = np.concatenate(([0.0], np.cumsum(row_strip)))
                seg_sums = cum[cidx[1:]] - cum[cidx[:-1]]
                seg_lens = (cidx[1:] - cidx[:-1]).astype(np.float32)
                h_src[r] = seg_sums / np.maximum(seg_lens, 1)
            print(f"    band {r0}..{r1}: source rows {sr0}..{sr1}", end="\r")
            del arr
    print()
    return h_src


def main():
    print("Loading override sources...")
    vec_img  = Image.open(VEC)
    base_img = Image.open(BASE)
    vec  = np.array(vec_img.split()[0] if vec_img.mode in ("RGB", "RGBA")
                    else vec_img.convert("L"), dtype=np.uint8)
    base = np.array(base_img.split()[0] if base_img.mode in ("RGB", "RGBA")
                    else base_img.convert("L"), dtype=np.uint8)
    if base.shape != vec.shape:
        base = np.array(Image.fromarray(base).resize(vec.shape[::-1], Image.NEAREST),
                        dtype=np.uint8)
    composite = np.where(vec > 0, vec, base).astype(np.uint8)
    H, W = composite.shape
    print(f"  composite: {composite.shape}")
    print(f"  zones present: {sorted(np.unique(composite).tolist())}")

    print("Downsampling height.tif (50k -> 8192)...")
    h_src = downsample_height(H, W)
    raw2y = raw_to_mcy_spline()
    mc_y = raw2y(h_src.astype(np.float64)).astype(np.float32)
    print(f"  MC Y range: {mc_y.min():.0f} .. {mc_y.max():.0f}")

    cold_mask = np.isin(composite, np.array(COLD, dtype=np.uint8))
    cold_n = int(cold_mask.sum())
    print(f"\nCold-zone pixels: {cold_n:,} ({cold_n / composite.size * 100:.2f}% of source)")

    # Snapshot before/after counts
    print("\nBefore banding:")
    for z, name in [(BT, "BT"), (SBT, "SBT"), (BA, "BA"), (AT, "AT")]:
        n = int((composite == z).sum())
        print(f"  {name:>3} ({z}): {n:>10,} ({n / cold_n * 100:>5.1f}% of cold)")

    # Apply banding only to cold-zone pixels (FF + warm biomes untouched)
    print(f"\nApplying banding: T1={T1}, T2={T2}, T3={T3}")
    cold = cold_mask
    in_low = cold & (mc_y < T1)
    in_mid = cold & (mc_y >= T1) & (mc_y < T2)
    in_high = cold & (mc_y >= T2) & (mc_y < T3)
    in_peak = cold & (mc_y >= T3)

    # Track per-zone reassignment (before -> after) for the report
    before = composite.copy()
    composite[in_low] = BA
    composite[in_mid] = BT
    composite[in_high] = SBT
    composite[in_peak] = AT

    # Pixel-change matrix
    print("\nReassignment matrix (rows = before, cols = after, in cold-zone pixels):")
    print(f"  {'':>8} | {'BA':>10} {'BT':>10} {'SBT':>10} {'AT':>10}  | {'total':>10}")
    for z, name in [(BA, "BA"), (BT, "BT"), (SBT, "SBT"), (AT, "AT")]:
        row = before == z
        row_total = int(row.sum())
        cells = []
        for zz, _ in [(BA, "BA"), (BT, "BT"), (SBT, "SBT"), (AT, "AT")]:
            n = int((row & (composite == zz)).sum())
            cells.append(f"{n:>10,}")
        print(f"  {name:>8} | {' '.join(cells)}  | {row_total:>10,}")

    # After-state coverage
    print("\nAfter banding:")
    for z, name in [(BT, "BT"), (SBT, "SBT"), (BA, "BA"), (AT, "AT")]:
        n = int((composite == z).sum())
        print(f"  {name:>3} ({z}): {n:>10,} ({n / cold_n * 100:>5.1f}% of cold)")

    # Verify FF + warm biomes untouched
    untouched_diff = int((before != composite).sum() - cold_n +
                         int(((before != composite) & ~cold).sum()))
    # Simpler: changed-outside-cold
    changed_outside = int(((before != composite) & ~cold).sum())
    print(f"\nSanity: changed pixels outside cold zone = {changed_outside} (must be 0)")
    assert changed_outside == 0, "Banding pass touched non-cold-zone pixels!"

    # Save as PNG (mode L for single-channel zone codes)
    print(f"\nSaving {OUT}...")
    Image.fromarray(composite, mode="L").save(OUT)
    size_kb = OUT.stat().st_size / 1024
    print(f"  written: {OUT.name} ({size_kb:.0f} KB)")
    print("\nNext step: run tools/upscale_override_BT_banded.py to upscale to 50k.")


if __name__ == "__main__":
    main()
