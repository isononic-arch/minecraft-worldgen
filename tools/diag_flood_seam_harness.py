"""S94 seam harness: load the flood-settle PADDED inputs dumped for two adjacent
tiles and run settle() with the OLD (dist) vs NEW (source) band ordering, then
measure SAME-WORLD-CELL agreement in the tiles' overlapping halos. A
seam-deterministic settle produces identical water at any shared world cell ->
0 mismatches. Proves whether the source-ordering fix closes the seam.

Usage (horizontal seam, north tile (tx,tzN), south (tx,tzN+1)):
  py tools/diag_flood_seam_harness.py <dump_dir> H <tx> <tzN>
Vertical (west (txW,tz), east (txW+1,tz)):
  py tools/diag_flood_seam_harness.py <dump_dir> V <txW> <tz>
"""
import sys
import numpy as np
from scipy import ndimage

PAD = 48
TS = 512
SEA = 63


def settle(source, bed, river, dist, skel, land, order_key):
    """Replica of core.river_flood_settle.settle with a switchable ordering
    key ('source' = new seam-fix, 'dist' = old per-tile)."""
    H, W = bed.shape
    bed = bed.astype(np.int64); source = source.astype(np.int64)
    river = river.astype(bool); land = land.astype(bool)
    inv = ~skel.astype(bool)
    _, (iy, ix) = ndimage.distance_transform_edt(inv, return_indices=True)
    label = iy.astype(np.int64) * W + ix.astype(np.int64)
    river_idx = np.where(river.ravel())[0]
    lab_r = label.ravel()[river_idx]; src_r = source.ravel()[river_idx]
    bands, inv_band = np.unique(lab_r, return_inverse=True); nb = len(bands)
    band_src = np.full(nb, np.iinfo(np.int64).max)
    np.minimum.at(band_src, inv_band, src_r)
    sy_pad = np.pad(bed, 1, constant_values=np.iinfo(np.int64).max)
    land_pad = np.pad(land, 1, constant_values=False)
    NOLAND = np.iinfo(np.int64).max
    min_land_adj = np.full((H, W), NOLAND, np.int64)
    for dz, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        nb_sy = sy_pad[1+dz:1+dz+H, 1+dx:1+dx+W]
        nb_is_land = land_pad[1+dz:1+dz+H, 1+dx:1+dx+W]
        min_land_adj = np.minimum(min_land_adj, np.where(nb_is_land, nb_sy, NOLAND))
    min_land_r = min_land_adj.ravel()[river_idx]
    band_bank = np.full(nb, NOLAND); np.minimum.at(band_bank, inv_band, min_land_r)
    band_level = np.minimum(band_src, band_bank)
    # --- ordering key ---
    if order_key == "source":
        order = np.argsort(-band_src.astype(np.float64))
    else:
        dist_r = dist.ravel()[river_idx].astype(np.float64)
        bsum = np.zeros(nb); bcnt = np.zeros(nb)
        np.add.at(bsum, inv_band, dist_r); np.add.at(bcnt, inv_band, 1.0)
        order = np.argsort(-(bsum / np.maximum(bcnt, 1.0)))
    if order_key == "nomono":
        band_level_mono = band_level.copy()
    else:
        run = np.minimum.accumulate(band_level[order])
        band_level_mono = np.empty_like(band_level); band_level_mono[order] = run
    # --- 4b relax ---
    cell_band = np.full(H*W, -1, np.int64); cell_band[river_idx] = inv_band
    cell_band = cell_band.reshape(H, W)
    bp = np.pad(cell_band, 1, constant_values=-1)
    if order_key == "source":
        kp = np.pad(source.astype(np.float64), 1, constant_values=-np.inf); here_k = source.astype(np.float64)
    else:
        kp = np.pad(dist.astype(np.float64), 1, constant_values=np.inf); here_k = dist.astype(np.float64)
    up_list = []; dn_list = []
    for dz, dx in ((-1,0),(1,0),(0,-1),(0,1)):
        nb_band = bp[1+dz:1+dz+H, 1+dx:1+dx+W]; nb_k = kp[1+dz:1+dz+H, 1+dx:1+dx+W]
        if order_key == "source":
            m = (cell_band>=0)&(nb_band>=0)&(nb_band!=cell_band)&(nb_k<here_k)
        else:
            m = (cell_band>=0)&(nb_band>=0)&(nb_band!=cell_band)&(nb_k<here_k)
        up_list.append(cell_band[m]); dn_list.append(nb_band[m])
    up_e = np.concatenate(up_list); dn_e = np.concatenate(dn_list)
    if up_e.size and order_key != "nomono":
        key = up_e.astype(np.int64)*nb + dn_e.astype(np.int64); key = np.unique(key)
        up_e = key // nb; dn_e = key % nb
        for _ in range(64):
            need = band_level_mono[up_e] < band_level_mono[dn_e]
            if not need.any(): break
            np.minimum.at(band_level_mono, dn_e, band_level_mono[up_e])
    out = np.full((H, W), -999, np.int64); out.ravel()[river_idx] = band_level_mono[inv_band]
    return out


def main(D, orient, ta, tb):
    if orient == "H":
        A = (ta, tb); B = (ta, tb+1)
        ozA = tb*TS - PAD; ozB = (tb+1)*TS - PAD; ox = ta*TS - PAD
        ozdim = 0
    else:
        A = (ta, tb); B = (ta+1, tb)
        oxA = ta*TS - PAD; oxB = (ta+1)*TS - PAD; oz = tb*TS - PAD
        ozdim = 1

    def ld(tag, t): return np.load(f"{D}/fs_{tag}_{t[0]}_{t[1]}.npy")

    def reversals(out, src, riv):
        """count river cells whose water is HIGHER than a strictly-downstream
        (lower-source) 4-neighbour -> water flowing uphill."""
        rev = 0
        for dz, dx in ((-1,0),(1,0),(0,-1),(0,1)):
            nb_o = np.roll(np.roll(out, dz, 0), dx, 1)
            nb_s = np.roll(np.roll(src, dz, 0), dx, 1)
            nb_r = np.roll(np.roll(riv, dz, 0), dx, 1)
            m = riv & nb_r & (out > SEA) & (nb_o > SEA) & (nb_s < src) & (out > nb_o)
            rev += int(m.sum())
        return rev
    res = {}
    for key in ("source", "dist", "nomono"):
        outs = {}; revs = 0
        for t in (A, B):
            outs[t] = settle(ld("src", t), ld("bed", t), ld("riv", t),
                             ld("dist", t), ld("skel", t), ld("land", t), key)
            revs += reversals(outs[t], ld("src", t).astype(np.int64), ld("riv", t).astype(bool))
        # overlap same-world-cell agreement
        if orient == "H":
            z0 = max(ozA, ozB); z1 = min(ozA+outs[A].shape[0], ozB+outs[B].shape[0])
            mis = 0; tot = 0
            for wz in range(z0, z1):
                arow = outs[A][wz-ozA]; brow = outs[B][wz-ozB]
                ra = ld("riv", A)[wz-ozA]; rb = ld("riv", B)[wz-ozB]
                both = ra & rb & (arow > SEA) & (brow > SEA)
                tot += int(both.sum()); mis += int(((arow != brow) & both).sum())
            # RENDERED seam = N's last inner row (world (tb+1)*TS-1) vs S's first
            # inner row ((tb+1)*TS) -- the two adjacent rows that actually render.
            wz_n = (tb+1)*TS - 1; wz_s = (tb+1)*TS
            n_in = outs[A][wz_n-ozA]; s_in = outs[B][wz_s-ozB]
            rn = ld("riv", A)[wz_n-ozA]; rs = ld("riv", B)[wz_s-ozB]
            bns = rn & rs & (n_in > SEA) & (s_in > SEA)
            rd = (n_in.astype(np.int64) - s_in.astype(np.int64))[bns]
            seam = (int(bns.sum()), int((rd != 0).sum()),
                    sorted(set(rd.tolist())) if bns.any() else [])
        res[key] = (mis, tot, seam, revs)
    print(f"=== flood-settle seam agreement, {orient} {A}|{B} ===")
    for key in ("dist", "source", "nomono"):
        mis, tot, seam, revs = res[key]
        b, d, deltas = seam
        print(f"  order={key:>6}: halo-determinism MISMATCHES={mis}/{tot}  "
              f"uphill-reversals={revs}")
        print(f"      RENDERED seam (N row vs S row): {d}/{b} columns differ, "
              f"step values {deltas}")
    print("\n  RENDERED seam = what you walk. 0 columns differ => seam GONE.")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
