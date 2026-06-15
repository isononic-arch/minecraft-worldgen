"""rebuild_river_wl.py — S94 GLOBAL river water-level bake (EDT-FREE, the seam fix).

Computes the river water surface ONCE at full 50k resolution so every tile reads
an identical, seam-continuous level. ONLY the LEVEL is global; the river EXTENT
stays per-tile (chunk_writer fills where level>bed at full res), so the shoreline
stays organic — no NEAREST "swimming pool" geometry.

WHY EDT-FREE: the first attempt called scipy.distance_transform_edt on the full
50k grid (2.5e9 cells) for nearest-centerline grouping + ocean distance — that did
not finish in 90 min and got the box auto-killed. But the only thing actually
needed is a nearest-centerline LABEL for the few-million river cells, which a
sparse cKDTree gives in seconds. Bank detection is np.roll (no EDT). Monotone
ordering is by the carver SOURCE level (a global, monotone-along-flow field) so no
ocean-distance EDT is needed. Footprint coverage is a single binary_dilation
(iterations), not an indexed EDT. Result: seconds-to-minutes at 50k.

Reproduces the per-tile flood-settle's band logic (min(source, lowest adjacent
land bank), flat per cross-section, monotone non-increasing downstream) — but
GLOBALLY, so it is seam-clean by construction and (being full-res) matches the
approved per-tile water height.

Inputs (50k masks): height, hydro_centerline, hydro_width, hydro_lake.
Output: masks/hydro_river_wl.tif (int16 water-surface MC-Y on the river footprint,
-999 elsewhere).

Usage:  python rebuild_river_wl.py [--scale 1] [--masks masks] [--cover 28]
  --scale 1 = full 50k (production, ~20GB peak, run on the 192GB box).
  --scale 4 = 12500 (fits the 7.5GB local box, for offline height-match validation).
"""
import argparse
import sys
import numpy as np
import rasterio
from rasterio.enums import Resampling
from scipy import ndimage
from scipy.spatial import cKDTree

sys.path.insert(0, ".")
from core import column_generator as _cg

LUT = _cg._LUT
SEA = _cg.SEA_LEVEL
FULL = 50000


def _read_full(path):
    with rasterio.open(path) as s:
        return s.read(1)


def _read_bilinear(path, n):
    with rasterio.open(path) as s:
        return s.read(1, out_shape=(n, n), resampling=Resampling.bilinear)


def _block_max(path, n, scale):
    out = np.zeros((n, n), dtype=np.float32)
    CH = max(1, 800 // scale)
    with rasterio.open(path) as s:
        W = s.width; WT = (W // scale) * scale
        for o0 in range(0, n, CH):
            o1 = min(o0 + CH, n)
            blk = s.read(1, window=((o0*scale, o1*scale), (0, WT))).astype(np.float32)
            nr = o1 - o0
            blk = blk[:nr*scale, :WT].reshape(nr, scale, WT // scale, scale)
            out[o0:o1, :WT // scale] = blk.max(axis=(1, 3))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=1)
    ap.add_argument("--masks", default="masks")
    ap.add_argument("--out", default="masks/hydro_river_wl.tif")
    ap.add_argument("--cover", type=int, default=28,
                    help="blocks to extend the level footprint beyond width/2")
    ap.add_argument("--native", action="store_true",
                    help="write at native (scaled) resolution; skip 50k upscale "
                         "(for local validation — avoids the 50k OOM)")
    a = ap.parse_args()
    sc = a.scale
    N = FULL // sc
    M = a.masks
    print(f"[river_wl] scale={sc} grid={N}x{N}", flush=True)

    if sc == 1:
        h = _read_full(f"{M}/height.tif").astype(np.uint16)
        cl = (_read_full(f"{M}/hydro_centerline.tif") > 0)
        wid = _read_full(f"{M}/hydro_width.tif").astype(np.float32)
        lake = (_read_full(f"{M}/hydro_lake.tif") > 0)
    else:
        h = _read_bilinear(f"{M}/height.tif", N).astype(np.uint16)
        cl = _block_max(f"{M}/hydro_centerline.tif", N, sc) > 0
        wid = _block_max(f"{M}/hydro_width.tif", N, sc)
        lake = _block_max(f"{M}/hydro_lake.tif", N, sc) > 0

    bed = LUT[h].astype(np.int32)
    del h
    cl &= ~lake & (bed > SEA)            # above-sea river centerline only
    print(f"[river_wl] bed built; centerline cells={int(cl.sum())}", flush=True)

    # --- centerline nodes (sparse) ------------------------------------------
    cl_r, cl_c = np.where(cl)
    if cl_r.size == 0:
        print("[river_wl] no above-sea centerline; nothing to bake", flush=True)
        return
    node_pts = np.column_stack([cl_r, cl_c])
    tree = cKDTree(node_pts)
    cover_px = max(1, a.cover // sc)

    # --- river footprint = centerline dilated by width/2 + cover (single
    #     dilation, no indexed EDT) ----------------------------------------
    maxw = float(np.percentile(wid[cl], 99)) if cl.any() else 1.0
    rad = int(np.ceil(maxw / 2.0 / sc)) + cover_px
    rad = int(np.clip(rad, 1, 64))
    foot = ndimage.binary_dilation(cl, iterations=rad) & ~lake
    print(f"[river_wl] footprint dilate r={rad}px -> {int(foot.sum())} cells", flush=True)

    fr, fc = np.where(foot)
    # nearest centerline node (band) for every footprint cell (sparse KDTree)
    dist_to_cl, lab = tree.query(np.column_stack([fr, fc]), workers=-1)
    nb = node_pts.shape[0]

    # --- per-cell source + min adjacent LAND bank (np.roll, no EDT) ----------
    source_full = np.where(foot, bed - 1, np.iinfo(np.int32).max).astype(np.int32)
    land = ~foot & ~lake
    INFB = np.iinfo(np.int32).max
    bed_land = np.where(land, bed, INFB).astype(np.int32)
    min_adj_land = np.full(bed.shape, INFB, np.int32)
    for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        min_adj_land = np.minimum(min_adj_land, np.roll(np.roll(bed_land, dz, 0), dx, 1))
    del bed_land, land

    src_f = (bed[fr, fc] - 1).astype(np.int64)
    bank_f = min_adj_land[fr, fc].astype(np.int64)
    # per-band (cross-section) aggregates
    band_src = np.full(nb, np.iinfo(np.int64).max); np.minimum.at(band_src, lab, src_f)
    band_bank = np.full(nb, np.iinfo(np.int64).max); np.minimum.at(band_bank, lab, bank_f)
    band_level = np.minimum(band_src, band_bank)
    del min_adj_land

    # --- monotone non-increasing downstream, ordered by SOURCE (global) ------
    order = np.argsort(-band_src.astype(np.float64))      # high source = upstream
    run = np.minimum.accumulate(band_level[order])
    band_mono = np.empty_like(band_level); band_mono[order] = run

    # --- local band-adjacency relaxation (fix reversals) --------------------
    # find adjacent footprint cells with different bands -> directed edge by src
    fl_grid = np.full(bed.shape, -1, np.int64); fl_grid[fr, fc] = lab
    src_grid = np.where(foot, bed - 1, -(1 << 30)).astype(np.int64)
    up_e = []; dn_e = []
    for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        nbl = np.roll(np.roll(fl_grid, dz, 0), dx, 1)
        nbs = np.roll(np.roll(src_grid, dz, 0), dx, 1)
        m = (fl_grid >= 0) & (nbl >= 0) & (nbl != fl_grid) & (nbs < src_grid)
        up_e.append(fl_grid[m]); dn_e.append(nbl[m])
    up_e = np.concatenate(up_e); dn_e = np.concatenate(dn_e)
    if up_e.size:
        key = np.unique(up_e * nb + dn_e); up_e = key // nb; dn_e = key % nb
        for _ in range(64):
            need = band_mono[up_e] < band_mono[dn_e]
            if not need.any():
                break
            np.minimum.at(band_mono, dn_e, band_mono[up_e])
    del fl_grid, src_grid

    # --- scatter level to the footprint cells -------------------------------
    out = np.full(bed.shape, -999, np.int16)
    lvl_f = band_mono[lab]
    out[fr, fc] = np.where(lvl_f > SEA, lvl_f, -999).astype(np.int16)
    print(f"[river_wl] water cells={int((out > SEA).sum())}", flush=True)

    # --- upscale to 50k unless --native (local validation keeps native res) --
    if sc != 1 and not a.native:
        out = np.repeat(np.repeat(out, sc, 0), sc, 1)[:FULL, :FULL]
    with rasterio.open(f"{M}/hydro_centerline.tif") as ref:
        prof = ref.profile
    prof.update(dtype="int16", count=1, compress="deflate", nodata=-999,
                width=out.shape[1], height=out.shape[0])
    with rasterio.open(a.out, "w", **prof) as dst:
        dst.write(out.astype(np.int16), 1)
    print(f"[river_wl] WROTE {a.out} shape={out.shape}", flush=True)


if __name__ == "__main__":
    main()
