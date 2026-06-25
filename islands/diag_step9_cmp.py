"""Compare sy_final (post Step-9 locks) vs sy_postdec (post-decorate, relief
included) at the shared edge of two adjacent tiles. Confirms whether the Step-9
lock-restoration introduces a seam absent in the decorate output.

Usage: py diag_step9_cmp.py <dump_dir> <ltxA> <ltyA> <ltxB> <ltyB> [axis x|z]
"""
import sys, numpy as np
d = sys.argv[1]
ax, ay, bx, by = map(int, sys.argv[2:6])
axis = sys.argv[6] if len(sys.argv) > 6 else "x"

finA = np.load(f"{d}/sy_final_{ax}_{ay}.npy")
finB = np.load(f"{d}/sy_final_{bx}_{by}.npy")
pdA = np.load(f"{d}/sy_postdec_{ax}_{ay}.npy")
pdB = np.load(f"{d}/sy_postdec_{bx}_{by}.npy")

if axis == "x":   # A left, B right
    fA, fB = finA[:, -1], finB[:, 0]
    pA, pB = pdA[:, -1], pdB[:, 0]
else:             # A top, B bottom
    fA, fB = finA[-1, :], finB[0, :]
    pA, pB = pdA[-1, :], pdB[0, :]

land = (fA > 63) & (fB > 63)
print(f"edge land cells={int(land.sum())}")


def stats(name, a, b, m):
    dd = np.abs(a[m].astype(float) - b[m].astype(float))
    print(f"  {name:24s} seam mean={dd.mean():.3f} p95={np.percentile(dd,95):.2f} "
          f"max={dd.max():.1f} ge3={int((dd>=3).sum())}/{int(m.sum())}")


stats("FINAL (post Step-9)", fA, fB, land)
stats("POSTDEC (post-decorate)", pA, pB, land)

# per-side: how much did Step-9 change the EDGE column vs decorate?
chgA = np.abs(fA.astype(float) - pA.astype(float))
chgB = np.abs(fB.astype(float) - pB.astype(float))
print(f"\nStep-9 changed edge col A: mean={chgA[land].mean():.3f} max={chgA[land].max():.1f} ge1={int((chgA[land]>=1).sum())}")
print(f"Step-9 changed edge col B: mean={chgB[land].mean():.3f} max={chgB[land].max():.1f} ge1={int((chgB[land]>=1).sum())}")

# worst FINAL-seam cells; show whether postdec was clean there + which side Step-9 moved
dfin = np.abs(fA.astype(float) - fB.astype(float))
worst = np.argsort(dfin * land)[::-1][:10]
print("\nworst FINAL-seam cells (row, finStep, postdecStep, chgA, chgB, fA, fB, pA, pB):")
for w in worst:
    if not land[w]:
        continue
    pdstep = abs(float(pA[w]) - float(pB[w]))
    print(f"  r={w:3d} fin={dfin[w]:.0f} postdec={pdstep:.0f}  chgA={chgA[w]:.0f} chgB={chgB[w]:.0f}"
          f"  fA={fA[w]:.0f} fB={fB[w]:.0f}  pA={pA[w]:.0f} pB={pB[w]:.0f}")
