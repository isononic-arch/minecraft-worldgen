"""S94: settle the (84,60) wide-channel patchwork cause using the carver's
INTERNAL fields (cdist=_dist_from_ocean, cwatery=final water_y, cskel, criv).

Answers:
  A. Is water level a clean function of the carver's dist? (level range per
     dist-isoline). ~0 => yes; >0 => propagation assigns same-dist cells
     different levels.
  B. Is dist monotone & cross-section-constant, or does the meander make it
     non-monotone along flow? Compare dist vs skeleton GEODESIC arc-length.
  C. Per cross-section (nearest-skeleton bucket), spread of dist vs arc-length.

Usage: py tools/diag_river_coord.py <dir> <tx> <tz>
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from scipy.ndimage import distance_transform_edt


def geodesic_along_skel(skel, seed_rc):
    """BFS geodesic distance along skeleton pixels (8-connected) from seed."""
    from collections import deque
    H, W = skel.shape
    dist = np.full((H, W), -1.0, np.float32)
    sr, sc = seed_rc
    dist[sr, sc] = 0.0
    dq = deque([(sr, sc)])
    while dq:
        r, c = dq.popleft()
        base = dist[r, c]
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = r + dr, c + dc
                if 0 <= nr < H and 0 <= nc < W and skel[nr, nc] and dist[nr, nc] < 0:
                    dist[nr, nc] = base + (1.4142 if dr and dc else 1.0)
                    dq.append((nr, nc))
    return dist


def main(d, tx, tz):
    cdist = np.load(f"{d}/cdist_{tx}_{tz}.npy")
    wy = np.load(f"{d}/cwatery_{tx}_{tz}.npy").astype(np.int32)
    riv = np.load(f"{d}/criv_{tx}_{tz}.npy").astype(bool)
    skel = np.load(f"{d}/cskel_{tx}_{tz}.npy").astype(bool)
    rivw = riv & (wy > 63)
    n = int(rivw.sum())
    print(f"({tx},{tz}) river cells (water>63): {n}, skeleton cells: {int(skel.sum())}")

    # A. level range per carver-dist isoline
    db = np.round(cdist).astype(int)
    sp = []
    for b in np.unique(db[rivw]):
        m = rivw & (db == b)
        if m.sum() < 5:
            continue
        sp.append(int(wy[m].max() - wy[m].min()))
    sp = np.array(sp)
    print(f"A. level range per CARVER-dist isoline: mean={sp.mean():.2f} "
          f"max={sp.max()}  >=2: {(sp>=2).sum()}/{len(sp)}")
    print("   (0 => level is clean f(dist); >0 => propagation breaks same-dist uniformity)")

    # B. dist vs skeleton arc-length
    if skel.sum() > 2:
        sr, sc = np.where(skel)
        seed_i = int(np.argmin(cdist[sr, sc]))   # mouth = min dist
        arc = geodesic_along_skel(skel, (sr[seed_i], sc[seed_i]))
        sk_arc = arc[sr, sc]
        sk_dist = cdist[sr, sc]
        good = sk_arc >= 0
        # monotonicity of dist along arc-length: sort by arc, count dist reversals
        o = np.argsort(sk_arc[good])
        dseq = sk_dist[good][o]
        drops = np.diff(dseq)
        reversals = int((drops < -0.5).sum())   # dist DECREASES then must re-increase
        print(f"B. skeleton arc-length range: {sk_arc[good].max():.0f}; "
              f"dist-vs-arc reversals (meander non-monotonicity): {reversals} "
              f"of {good.sum()-1} steps ({100.0*reversals/max(1,good.sum()-1):.1f}%)")
        print(f"   corr(dist,arc)={np.corrcoef(sk_dist[good], sk_arc[good])[0,1]:.3f} "
              f"(1.0 => dist is a faithful flow coordinate; <1 => meander breaks it)")

        # C. per cross-section: nearest skeleton cell -> its arc & dist
        _, idx = distance_transform_edt(~skel, return_indices=True)
        near_arc = arc[idx[0], idx[1]]
        near_dist = cdist[idx[0], idx[1]]
        # bucket river cells by nearest-skel arc (cross-section id), measure
        # the spread of the cell's OWN cdist within each bucket
        ab = np.round(near_arc).astype(int)
        dspread, lspread = [], []
        for b in np.unique(ab[rivw]):
            m = rivw & (ab == b)
            if m.sum() < 5:
                continue
            dspread.append(float(cdist[m].max() - cdist[m].min()))
            lspread.append(int(wy[m].max() - wy[m].min()))
        dspread = np.array(dspread); lspread = np.array(lspread)
        print(f"C. per cross-section (nearest-skel arc bucket): "
              f"cdist spread mean={dspread.mean():.1f} p90={np.percentile(dspread,90):.0f} "
              f"max={dspread.max():.0f}")
        print(f"   level spread within a cross-section: mean={lspread.mean():.2f} "
              f"max={lspread.max()}  >=2: {(lspread>=2).sum()}/{len(lspread)}")
        print("   => if cdist spread is large but arc-bucket is one cross-section,"
              " keying level to ARC instead of DIST removes the patchwork.")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
