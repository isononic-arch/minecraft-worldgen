"""Compare river water-Y between two Step-9 dumps (old vs new carver) OR just
characterize one. Reports floating lips, cross-channel spread, band count,
and per-cell drift if both dirs given.
Usage: py tools/diag_water_compare.py <tx> <tz> <new_dir> [old_dir]"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from scipy.ndimage import label

def load(d, tx, tz):
    rwy = np.load(f"{d}/rwy_{tx}_{tz}.npy").astype(np.int32)
    rm = np.load(f"{d}/rmeta9_{tx}_{tz}.npy")
    riv = ((rm == 1) | (rm == 2)) & (rwy > 63)
    return rwy, riv

def lips(rwy, riv):
    big = np.int32(1 << 20); rv = np.where(riv, rwy, big)
    n4max = np.maximum.reduce([np.roll(rv,1,0),np.roll(rv,-1,0),np.roll(rv,1,1),np.roll(rv,-1,1)])
    n4max = np.where(n4max >= big, -big, n4max)
    return int((riv & (rwy > n4max)).sum())

def spread(rwy, riv):
    big = np.int32(1<<20); rv = np.where(riv, rwy, big); d = np.zeros_like(rwy)
    for dr in (-1,0,1):
        for dc in (-1,0,1):
            if dr==dc==0: continue
            sh = np.roll(np.roll(rv,dr,0),dc,1); ok = riv & (sh<big)
            d = np.where(ok, np.maximum(d, np.abs(rwy-sh)), d)
    s = d[riv]; n = max(1, riv.sum())
    return (s>=2).sum(), 100.0*(s>=2).sum()/n

def chars(tag, rwy, riv):
    n = int(riv.sum())
    if n == 0:
        print(f"  [{tag}] no above-sea river"); return
    ge2, pct = spread(rwy, riv)
    lp = lips(rwy, riv)
    nlev = len(np.unique(rwy[riv]))
    plat = sum(label(riv & (rwy==v))[1] for v in np.unique(rwy[riv]))
    print(f"  [{tag}] cells={n} levels={nlev} plateaus={plat} "
          f"floating_lips={lp} spread>=2={ge2}({pct:.1f}%)")

def main(tx, tz, new_dir, old_dir=None):
    print(f"=== ({tx},{tz}) ===")
    rwy_n, riv_n = load(new_dir, tx, tz)
    if old_dir:
        rwy_o, riv_o = load(old_dir, tx, tz)
        chars("OLD", rwy_o, riv_o)
    chars("NEW", rwy_n, riv_n)
    if old_dir:
        both = riv_n & riv_o
        if both.any():
            drift = (rwy_n - rwy_o)[both]
            print(f"  DRIFT (new-old, shared cells={both.sum()}): "
                  f"==0:{100.0*(drift==0).sum()/both.sum():.1f}% "
                  f"|>=1|:{100.0*(np.abs(drift)>=1).sum()/both.sum():.1f}% "
                  f"max|{np.abs(drift).max()}| mean{drift.mean():+.2f}")
        print(f"  river-cell count old={int(riv_o.sum())} new={int(riv_n.sum())} "
              f"(delta {int(riv_n.sum())-int(riv_o.sum())})")

if __name__ == "__main__":
    a = sys.argv
    main(int(a[1]), int(a[2]), a[3], a[4] if len(a) > 4 else None)
