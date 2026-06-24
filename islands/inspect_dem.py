"""inspect_dem.py — examine a real-world DEM PNG before deriving island masks.

Reports dims/dtype, elevation distribution, a guessed sea-level cut, connected
landmass components (bbox + area + peak), and writes downscaled previews
(grayscale + hillshade + land/sea) so we can SEE what islands are in the file.

Usage:  py islands/inspect_dem.py "<path-to-dem.png>" [--out islands/preview]
"""
import argparse
from pathlib import Path
import numpy as np
from PIL import Image


def load(path):
    im = Image.open(path)
    mode = im.mode
    if mode not in ("I", "I;16", "I;16B", "L", "F"):
        im = im.convert("I")
    return np.asarray(im).astype(np.float64), mode


def hillshade(z, az=315, alt=45):
    az, alt = np.radians(az), np.radians(alt)
    gy, gx = np.gradient(z)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    sh = (np.sin(alt) * np.cos(slope)
          + np.cos(alt) * np.sin(slope) * np.cos(az - aspect))
    return np.clip(sh, 0, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dem")
    ap.add_argument("--out", default="islands/preview")
    a = ap.parse_args()

    z, mode = load(a.dem)
    H, W = z.shape
    print(f"file   : {a.dem}")
    print(f"dims   : {W}x{H}   PIL mode={mode}")
    print(f"raw    : min={z.min():.0f} max={z.max():.0f} mean={z.mean():.1f}")
    qs = np.percentile(z, [0, 1, 5, 25, 50, 75, 90, 95, 99, 100])
    print("pctile : " + "  ".join(f"p{p}={v:.0f}" for p, v in
          zip([0, 1, 5, 25, 50, 75, 90, 95, 99, 100], qs)))

    # Guess sea level: real-world DEMs of islands have a huge flat ocean floor at
    # (or near) the minimum. Pick the modal low value as sea.
    lo = z[z <= np.percentile(z, 60)]
    hist, edges = np.histogram(lo, bins=256)
    sea_raw = (edges[hist.argmax()] + edges[hist.argmax() + 1]) / 2
    frac_at_sea = float(np.mean(np.abs(z - sea_raw) <= (z.max() - z.min()) * 0.01))
    print(f"sea?   : modal-low raw~{sea_raw:.0f}  "
          f"({frac_at_sea*100:.1f}% of pixels within 1% of it)")

    land = z > sea_raw + (z.max() - sea_raw) * 0.02   # 2% above sea = land
    print(f"land   : {land.mean()*100:.2f}% of pixels above sea")

    # connected components (4-conn) via scipy
    from scipy import ndimage
    lab, n = ndimage.label(land)
    sizes = ndimage.sum(np.ones_like(lab), lab, index=range(1, n + 1))
    order = np.argsort(sizes)[::-1]
    print(f"\nlandmasses: {n} components; top by area:")
    for k in order[:8]:
        cid = k + 1
        ys, xs = np.where(lab == cid)
        if len(xs) < 50:
            continue
        peak = z[ys, xs].max()
        print(f"  #{cid:<4} area={int(sizes[k]):>9} px  "
              f"bbox x[{xs.min()},{xs.max()}] y[{ys.min()},{ys.max()}]  "
              f"({xs.max()-xs.min()}x{ys.max()-ys.min()})  peak_raw={peak:.0f}")

    # previews (downsample 8x)
    outdir = Path(a.out); outdir.mkdir(parents=True, exist_ok=True)
    ds = z[::8, ::8]
    g = ((ds - ds.min()) / (np.ptp(ds) + 1e-9) * 255).astype(np.uint8)
    Image.fromarray(g, "L").save(outdir / "gray.png")
    hs = (hillshade(ds) * 255).astype(np.uint8)
    Image.fromarray(hs, "L").save(outdir / "hillshade.png")
    lm = (land[::8, ::8] * 255).astype(np.uint8)
    Image.fromarray(lm, "L").save(outdir / "landsea.png")
    print(f"\npreviews -> {outdir}/gray.png, hillshade.png, landsea.png ({g.shape[1]}x{g.shape[0]})")


if __name__ == "__main__":
    main()
