"""S94 blob-cleanup prototype: detect connected water-level BLOBS that are
local maxima (water higher than ALL surrounding river water) and lower them to
the surrounding level. Preserves legitimate terraces (they have a higher
upstream neighbour) and the source (guarded). Only LOWERS. 1-cell ring blend.
Tests offline on a Step-9 dump's rendered water (rwy).
Usage: py tools/diag_s94_blob_cleanup.py <dump_dir> <tx> <tz>
"""
import sys, numpy as np
from scipy.ndimage import label, binary_dilation, grey_erosion

def neighbours_label(lab, n):
    """For each label, the set of adjacent labels (4-conn)."""
    adj = {i: set() for i in range(1, n + 1)}
    for dr, dc in ((1,0),(-1,0),(0,1),(0,-1)):
        sh = np.roll(lab, (dr, dc), (0, 1))
        m = (lab > 0) & (sh > 0) & (lab != sh)
        for a, b in zip(lab[m], sh[m]):
            adj[int(a)].add(int(b))
    return adj

def blob_cleanup(wy, riv, max_iter=20, ring_blend=True):
    wy = wy.copy().astype(np.int32)
    rivf = riv & (wy > 63)
    for _it in range(max_iter):
        # label connected equal-water patches within the river
        patches = np.zeros(wy.shape, np.int32)
        nextlab = 1
        levels = sorted(np.unique(wy[rivf]).tolist())
        plevel = {}
        for L in levels:
            m = rivf & (wy == L)
            lab, k = label(m)
            lab2 = np.where(lab > 0, lab + (nextlab - 1), 0)
            patches = np.where(lab > 0, lab2, patches)
            for j in range(1, k + 1):
                plevel[nextlab + j - 1] = L
            nextlab += k
        n = nextlab - 1
        if n == 0:
            break
        adj = neighbours_label(patches, n)
        global_max = max(plevel.values())
        # outlier blob = strict local max (level > all neighbour levels),
        # excluding the global-max (source) patches and patches touching the
        # tile edge (river continues off-tile = a real source/sink, not a bump)
        edge = np.zeros(wy.shape, bool); edge[0,:]=edge[-1,:]=edge[:,0]=edge[:,-1]=True
        edge_labels = set(patches[edge & (patches>0)].tolist())
        changed = False
        for p in range(1, n + 1):
            Lp = plevel[p]
            if Lp >= global_max:        # guard the source / highest tier
                continue
            if p in edge_labels:         # guard off-tile continuations
                continue
            nbrs = adj[p]
            if not nbrs:
                continue
            nbr_max = max(plevel[q] for q in nbrs)
            if Lp > nbr_max:             # blob pokes above ALL neighbours
                wy[patches == p] = nbr_max   # lower to highest neighbour (1 step)
                changed = True
        if not changed:
            break
    if ring_blend:
        # 1-cell ring smooth: any river cell that is now a strict local max
        # vs 4-nbrs -> drop to max river neighbour (cleans the patch rim)
        for _ in range(2):
            big = np.int32(1<<20); rv = np.where(rivf, wy, -big)
            n4 = np.maximum.reduce([np.roll(rv,1,0),np.roll(rv,-1,0),np.roll(rv,1,1),np.roll(rv,-1,1)])
            lip = rivf & (n4 > -big) & (wy > n4)
            if not lip.any(): break
            wy[lip] = n4[lip]
    return wy

def metrics(tag, wy, bed, riv):
    rivf = riv & (wy > 63); nn = int(rivf.sum())
    big = 1<<20; rv = np.where(rivf, wy, big); dd = np.zeros_like(wy)
    for dr in (-1,0,1):
        for dc in (-1,0,1):
            if dr==dc==0: continue
            sh = np.roll(np.roll(rv,dr,0),dc,1); ok = rivf & (sh<big)
            dd = np.where(ok, np.maximum(dd, np.abs(wy-sh)), dd)
    s = dd[rivf]; depth = (wy-bed)[rivf]
    print(f"  [{tag}] levels={len(np.unique(wy[rivf]))} uniform0={100*(s==0).sum()/nn:.0f}% "
          f">=2={100*(s>=2).sum()/nn:.0f}% maxspread={s.max()} depth p50={int(np.median(depth))} "
          f"min={int(depth.min())} drypokes={int((depth<1).sum())}")

if __name__ == "__main__":
    d, tx, tz = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    wy = np.load(f"{d}/rwy_{tx}_{tz}.npy").astype(np.int32)
    bed = np.load(f"{d}/sy_final_{tx}_{tz}.npy").astype(np.int32)
    rm = np.load(f"{d}/rmeta9_{tx}_{tz}.npy")
    riv = ((rm==1)|(rm==2)) & (wy>63)
    metrics("BEFORE (current rendered)", wy, bed, riv)
    new = blob_cleanup(wy, riv)
    metrics("AFTER blob-cleanup", new, bed, riv)
    ch = riv & (new != wy)
    print(f"  changed {int(ch.sum())} cells ({100*ch.sum()/max(1,riv.sum()):.1f}%), "
          f"all lowered: {bool((new[ch] <= wy[ch]).all())}, max drop {int((wy-new)[ch].max()) if ch.any() else 0}")
    np.save(f"{d}/rwy_blobclean_{tx}_{tz}.npy", new)
