"""Compare decorate-output (sy_post, relief-included) seam vs pre-decorate
(sy_pre) seam at the shared edge of two adjacent tiles. If sy_post is seam-clean
while the rendered MCA-final is seamed, the seam is introduced post-decorate
(the Step-9 lock restoration)."""
import sys, numpy as np
d = sys.argv[1]
ax, ay, bx, by = map(int, sys.argv[2:6])
axis = sys.argv[6] if len(sys.argv) > 6 else "x"

preA = np.load(f"{d}/sy_pre_{ax}_{ay}.npy"); preB = np.load(f"{d}/sy_pre_{bx}_{by}.npy")
postA = np.load(f"{d}/sy_post_{ax}_{ay}.npy"); postB = np.load(f"{d}/sy_post_{bx}_{by}.npy")

if axis == "x":
    eprA, eprB = preA[:, -1], preB[:, 0]
    epoA, epoB = postA[:, -1], postB[:, 0]
else:
    eprA, eprB = preA[-1, :], preB[0, :]
    epoA, epoB = postA[-1, :], postB[0, :]

land = (epoA > 63) & (epoB > 63)


def st(name, a, b, m):
    dd = np.abs(a[m].astype(float) - b[m].astype(float))
    print(f"  {name:28s} mean={dd.mean():.3f} p95={np.percentile(dd,95):.2f} max={dd.max():.1f} ge3={int((dd>=3).sum())}/{int(m.sum())}")


print(f"edge land cells={int(land.sum())}")
st("PRE-decorate seam", eprA, eprB, land)
st("POST-decorate (relief) seam", epoA, epoB, land)
# how much did decorate's relief move each side's edge?
mvA = np.abs(epoA.astype(float) - eprA.astype(float))
mvB = np.abs(epoB.astype(float) - eprB.astype(float))
print(f"  decorate moved edge A: mean={mvA[land].mean():.2f} max={mvA[land].max():.1f}")
print(f"  decorate moved edge B: mean={mvB[land].mean():.2f} max={mvB[land].max():.1f}")
