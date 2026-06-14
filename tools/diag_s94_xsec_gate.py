"""S94 cross-section-unify gate. Run on the dump dir.
(84,60): cross-section split (nearest-skel) -> ~0, depth preserved, no levees,
no dry-poke explosion. Seam (84,60)|(84,61) at z=31232: water continuous."""
import sys, numpy as np
from scipy.ndimage import distance_transform_edt, binary_dilation
d = sys.argv[1] if len(sys.argv) > 1 else "diag_s94_xsec"

def load(tx, tz, dd=d):
    rwy = np.load(f"{dd}/rwy_{tx}_{tz}.npy").astype(np.int32)
    sy = np.load(f"{dd}/sy_final_{tx}_{tz}.npy").astype(np.int32)
    rm = np.load(f"{dd}/rmeta9_{tx}_{tz}.npy")
    riv = ((rm == 1) | (rm == 2)) & (rwy > 63)
    return rwy, sy, riv

def split_metric(rwy, riv):
    # group by nearest-skel (true cross-section); within-group level range
    try:
        from skimage.morphology import skeletonize
        sk = skeletonize(riv)
    except Exception:
        sk = riv
    if not sk.any(): sk = riv
    _, idx = distance_transform_edt(~sk, return_indices=True)
    sid = idx[0].astype(np.int64) * 100003 + idx[1].astype(np.int64)
    ids = sid[riv]; w = rwy[riv]; sp = []
    for u in np.unique(ids):
        m = ids == u
        if m.sum() >= 2: sp.append(int(w[m].max() - w[m].min()))
    return np.array(sp) if sp else np.array([0])

def levees(rwy, sy, riv):
    land = ~riv; lh = np.where(land, sy, 99999)
    ln4 = np.minimum.reduce([np.roll(lh,1,0),np.roll(lh,-1,0),np.roll(lh,1,1),np.roll(lh,-1,1)])
    edge = riv & binary_dilation(land)
    return int((edge & (rwy > ln4)).sum())

print("=== (84,60) cross-section unify ===")
for dd, tag in [("diag_s94_rdiag", "BEFORE (baseline)"), (d, "AFTER (xsec-unify)")]:
    try:
        rwy, sy, riv = load(84, 60, dd)
    except FileNotFoundError:
        print(f"  [{tag}] dump missing"); continue
    sp = split_metric(rwy, riv); depth = (rwy - sy)[riv]
    print(f"  [{tag}] xsec_split mean={sp.mean():.2f} >=1:{100*(sp>=1).sum()/len(sp):.0f}% max={sp.max()} "
          f"| levels={len(np.unique(rwy[riv]))} depth_p50={int(np.median(depth))} "
          f"dry={int((depth<1).sum())} levees={levees(rwy,sy,riv)}")

print("=== seam (84,60)|(84,61) at z=31232 ===")
try:
    a = load(84, 60); b = load(84, 61)
    aE = a[0][511, :]; aR = a[2][511, :]   # (84,60) bottom row
    bE = b[0][0, :];   bR = b[2][0, :]     # (84,61) top row
    both = aR & bR; nb = int(both.sum())
    if nb:
        jump = np.abs(aE[both] - bE[both])
        print(f"  shared river cols: {nb}; |Δ| max={int(jump.max())} mean={jump.mean():.2f} "
              f">=2: {int((jump>=2).sum())} -> {'SEAM CLEAN' if jump.max()<=1 else 'SEAM JUMP'}")
    else:
        print("  river does not cross this seam")
except FileNotFoundError as e:
    print(f"  seam tile missing: {e}")
