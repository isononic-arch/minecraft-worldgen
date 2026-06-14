"""Render rwy water-Y as a heatmap + step-boundary + floating-lip overlay so we
can SEE the (84,60) unevenness the user walked. Usage: py ... <dump> <tx> <tz>"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from PIL import Image

def main(d, tx, tz):
    rwy = np.load(f"{d}/rwy_{tx}_{tz}.npy").astype(np.int32)
    rm = np.load(f"{d}/rmeta9_{tx}_{tz}.npy")
    riv = ((rm == 1) | (rm == 2)) & (rwy > 63)
    ys = rwy[riv]
    lo, hi = ys.min(), ys.max()
    # crop to river bbox + margin
    rr, cc = np.where(riv)
    r0, r1 = max(0, rr.min()-8), min(512, rr.max()+8)
    c0, c1 = max(0, cc.min()-8), min(512, cc.max()+8)
    H, W = r1-r0, c1-c0
    img = np.zeros((H, W, 3), np.uint8)
    img[:] = (25, 28, 32)
    # color each water cell by Y level with a banded colormap (distinct per int)
    palette = [(40,60,140),(50,110,180),(60,160,200),(80,200,180),
               (120,210,120),(190,210,90),(230,180,70),(235,120,60),
               (220,70,70),(180,50,120),(140,60,170)]
    sub = riv[r0:r1, c0:c1]
    rwc = rwy[r0:r1, c0:c1]
    for y in range(lo, hi+1):
        m = sub & (rwc == y)
        img[m] = palette[(y-lo) % len(palette)]
    # floating-lip overlay (local max among river 4-nbrs) in white
    big = np.int32(1<<20); rv = np.where(riv, rwy, big)
    n4max = np.maximum.reduce([np.roll(rv,1,0),np.roll(rv,-1,0),np.roll(rv,1,1),np.roll(rv,-1,1)])
    n4max = np.where(n4max>=big, -big, n4max)
    localmax = riv & (rwy > n4max)  # strictly above ALL river neighbours = true lip
    lm = localmax[r0:r1, c0:c1]
    img[lm] = (255,255,255)
    print(f"({tx},{tz}) river bbox {H}x{W}, Y {lo}..{hi}, true local-max lips: {int(localmax.sum())}")
    scale = max(1, 1400 // max(H, W))
    Image.fromarray(img).resize((W*scale, H*scale), Image.NEAREST).save(f"{d}/water_heatmap_{tx}_{tz}.png")
    print(f"  wrote {d}/water_heatmap_{tx}_{tz}.png (scale {scale}x); white=true floating lips")

if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
