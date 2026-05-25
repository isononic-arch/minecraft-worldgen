"""S86: Propose BT-banding elevation thresholds.

Reads current override (composite of override_vectorized + override_final at 8192),
samples height.tif (50k -> 8192 mean downsample), maps raw -> MC Y via terrain_spline,
then reports:
  - Y distribution of cold-zone pixels {BT 30, SBT 35, BA 40, AT 50}
  - Proposed 3 thresholds for 4 bands (lowland BA / midland BT / highland SBT / alpine AT)
  - Resulting coverage % per biome at proposed thresholds

Run: py tools/diag_BT_banding_propose.py
"""
import json
import numpy as np
from pathlib import Path
from PIL import Image
import rasterio
from scipy.interpolate import PchipInterpolator
from scipy.ndimage import zoom

ROOT = Path(r"C:\Users\nicho\minecraft-worldgen")
VEC  = ROOT / "override_vectorized.png"
BASE = ROOT / "override_final.png"
HEIGHT = ROOT / "masks" / "height.tif"
CFG  = ROOT / "config" / "thresholds.json"

COLD_ZONES = (30, 35, 40, 50)  # BT, SBT, BA, AT
BT, SBT, BA, AT = 30, 35, 40, 50


def raw_to_mcy_spline():
    sp = json.loads(CFG.read_text())["terrain_spline"]
    return PchipInterpolator(np.array(sp["gaea_in"], dtype=np.float64),
                             np.array(sp["mc_y_out"], dtype=np.float64))


def main():
    print("Loading override sources...")
    vec  = np.array(Image.open(VEC).split()[0], dtype=np.uint8)
    base = np.array(Image.open(BASE).split()[0] if Image.open(BASE).mode in ("RGB","RGBA")
                    else Image.open(BASE).convert("L"), dtype=np.uint8)
    if base.shape != vec.shape:
        base = np.array(Image.fromarray(base).resize(vec.shape[::-1], Image.NEAREST),
                        dtype=np.uint8)
    comp = np.where(vec > 0, vec, base).astype(np.uint8)
    H, W = comp.shape
    print(f"  composite: {comp.shape}, unique zones: {sorted(np.unique(comp).tolist())}")

    # Downsample height.tif (50k) -> matched to override (8192) via row-band block read
    print("Loading + downsampling height.tif (windowed)...")
    from rasterio.windows import Window
    h_src = np.zeros((H, W), dtype=np.float32)
    with rasterio.open(HEIGHT) as src:
        sh, sw = src.height, src.width  # 50000, 50000
        print(f"  src: {sh}x{sw}")
        # For each output row r in [0..H), read source rows mapping to that block
        # and column-block-average into h_src[r]
        BAND = 1024  # output rows per band -> source rows ~= BAND*sh/H
        for r0 in range(0, H, BAND):
            r1 = min(H, r0 + BAND)
            sr0 = int(r0 * sh / H)
            sr1 = int(r1 * sh / H)
            sh_band = sr1 - sr0
            arr = src.read(1, window=Window(0, sr0, sw, sh_band)).astype(np.float32)
            # Build column indices: each output col c maps to source col c*sw/W
            for r in range(r0, r1):
                src_r_lo = int((r - r0) * sh_band / (r1 - r0))
                src_r_hi = int((r - r0 + 1) * sh_band / (r1 - r0))
                if src_r_hi <= src_r_lo:
                    src_r_hi = src_r_lo + 1
                row_strip = arr[src_r_lo:src_r_hi].mean(axis=0)  # 1D len sw
                # Column block-mean from sw -> W
                # Reshape via index lookup (block-mean)
                cidx = np.linspace(0, sw, W + 1, dtype=np.int64)
                cum = np.concatenate(([0.0], np.cumsum(row_strip)))
                seg_sums = cum[cidx[1:]] - cum[cidx[:-1]]
                seg_lens = (cidx[1:] - cidx[:-1]).astype(np.float32)
                h_src[r] = seg_sums / np.maximum(seg_lens, 1)
            print(f"    band {r0}..{r1}: source rows {sr0}..{sr1}", end="\r")
            del arr
    print(f"\n  height downsampled: {h_src.shape}")

    # Convert raw -> MC Y
    raw2y = raw_to_mcy_spline()
    mc_y = raw2y(h_src.astype(np.float64)).astype(np.float32)
    print(f"  MC Y range: {mc_y.min():.0f} -> {mc_y.max():.0f}")

    # Cold-zone analysis
    cold_mask = np.isin(comp, COLD_ZONES)
    cold_y = mc_y[cold_mask]
    print(f"\nCold zone (BT+SBT+BA+AT): {cold_mask.sum():,} src px = {cold_mask.mean()*100:.2f}% of source")

    # Per-zone Y stats (current state)
    print("\nCurrent per-zone Y stats (cold-zone only):")
    print(f"{'Zone':<8} {'Name':<6} {'count':>10} {'%cold':>7} {'min':>4} {'p25':>4} {'med':>4} {'p75':>4} {'p95':>4} {'max':>4}")
    for z, name in [(BT, "BT"), (SBT, "SBT"), (BA, "BA"), (AT, "AT")]:
        zm = comp == z
        ys = mc_y[zm]
        if len(ys) == 0:
            print(f"{z:<8} {name:<6} {'(empty)':>10}")
            continue
        pct = zm.sum() / cold_mask.sum() * 100
        p = np.percentile(ys, [0, 25, 50, 75, 95, 100])
        print(f"{z:<8} {name:<6} {zm.sum():>10,} {pct:>6.1f}% "
              f"{p[0]:>4.0f} {p[1]:>4.0f} {p[2]:>4.0f} {p[3]:>4.0f} {p[4]:>4.0f} {p[5]:>4.0f}")

    # Cold-zone Y distribution overall
    print("\nCold-zone overall Y distribution (target for banding):")
    qs = [5, 10, 25, 33, 40, 50, 60, 66, 75, 80, 90, 95, 99]
    pcts = np.percentile(cold_y, qs)
    for q, v in zip(qs, pcts):
        print(f"  P{q:>2}: Y = {v:>4.0f}")

    # Propose thresholds based on land-realism + cold-zone shape:
    # BA lowland = sea-to-mid (target P0..P33 of cold zone, or absolute Y 63-200)
    # BT midland = mid-band  (P33..P66, or absolute Y 200-400)
    # SBT highland = upper band (P66..P95, or absolute Y 400-580)
    # AT peaks = top (P95+, or absolute Y > 580)
    print("\n=== Proposed banding (multiple schemes) ===")
    schemes = [
        ("Equal-area splits (cold zone)",
         np.percentile(cold_y, [33, 66, 95])),
        ("User spec: 75-200 / 200-400 / 400-600 / 600+",
         np.array([200, 400, 600], dtype=np.float64)),
        ("Conservative: 200 / 380 / 560",
         np.array([200, 380, 560], dtype=np.float64)),
        ("Aggressive low BA: 180 / 360 / 540",
         np.array([180, 360, 540], dtype=np.float64)),
    ]
    for name, thr in schemes:
        T1, T2, T3 = float(thr[0]), float(thr[1]), float(thr[2])
        ba_n = ((cold_y >= 63) & (cold_y < T1)).sum()
        bt_n = ((cold_y >= T1) & (cold_y < T2)).sum()
        sbt_n = ((cold_y >= T2) & (cold_y < T3)).sum()
        at_n = (cold_y >= T3).sum()
        below_sea = (cold_y < 63).sum()
        tot = len(cold_y)
        print(f"\n  {name}")
        print(f"  thresholds: lowland<{T1:.0f}  mid<{T2:.0f}  high<{T3:.0f}  peaks>={T3:.0f}")
        print(f"  BA  lowland: {ba_n:>10,} ({ba_n/tot*100:>5.1f}% of cold)")
        print(f"  BT  midland: {bt_n:>10,} ({bt_n/tot*100:>5.1f}% of cold)")
        print(f"  SBT highland:{sbt_n:>10,} ({sbt_n/tot*100:>5.1f}% of cold)")
        print(f"  AT  peaks:   {at_n:>10,} ({at_n/tot*100:>5.1f}% of cold)")
        if below_sea:
            print(f"  (below sea level: {below_sea:,} - will become ocean during prune)")

    # Save downsampled height + cold mask so the repaint script can reuse
    np.savez_compressed(ROOT / "diag_BT_banding_cache.npz",
                        comp=comp, mc_y=mc_y, cold_mask=cold_mask)
    print(f"\nCached: diag_BT_banding_cache.npz ({comp.size + mc_y.size*4 + cold_mask.size:,} bytes)")


if __name__ == "__main__":
    main()
