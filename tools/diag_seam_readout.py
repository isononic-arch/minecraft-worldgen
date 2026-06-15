"""Read the rendered block columns along a tile seam from BOTH region MCAs and
report terrain-surface + water-surface discontinuity per column. Classifies each
seam column as lake / river / land using the hydro masks so we can see whether
the jump is a lake-water-level mismatch or a terrain step.

Usage (horizontal seam between north tile (tx,tzN) and south tile (tx,tzN+1)):
  py tools/diag_seam_readout.py H <mca_dir> <tx> <tzN>
Usage (vertical seam between west tile (txW,tz) and east tile (txW+1,tz)):
  py tools/diag_seam_readout.py V <mca_dir> <txW> <tz>
"""
import sys, io, zlib, struct
import numpy as np
import nbtlib
import rasterio

WATER = {"water", "bubble_column", "kelp", "kelp_plant", "seagrass", "tall_seagrass"}


def read_chunk(mca_path, cx, cz):
    with open(mca_path, "rb") as f:
        header = f.read(4096)
        idx = (cx & 31) + (cz & 31) * 32
        off = struct.unpack(">I", b"\x00" + header[idx*4:idx*4+3])[0]
        if off == 0:
            return None
        f.seek(off * 4096)
        ln = struct.unpack(">I", f.read(4))[0]
        ctype = f.read(1)[0]
        raw = f.read(ln - 1)
        raw = zlib.decompress(raw) if ctype == 2 else raw
    return nbtlib.File.parse(io.BytesIO(raw))


def section_block(chunk, wy, lx, lz):
    secs = chunk.get("sections") or chunk.root.get("sections")
    sec_y = wy >> 4; ly = wy & 15
    for sec in secs:
        if int(sec.get("Y", -999)) != sec_y:
            continue
        bs = sec.get("block_states")
        if bs is None:
            return "air"
        pal = bs.get("palette")
        names = [str(p.get("Name", "air")).replace("minecraft:", "") for p in pal]
        if len(names) == 1:
            return names[0]
        data = bs.get("data")
        bits = max(4, (len(names) - 1).bit_length()); per = 64 // bits
        bi = (ly * 256) + (lz * 16) + lx
        L = int(data[bi // per]); shift = (bi % per) * bits
        return names[(L >> shift) & ((1 << bits) - 1)]
    return "air"


def col_scan(chunk, lx, lz, y_hi=200, y_lo=55):
    """Return (top_solid_y, top_water_y) for a column."""
    top_solid = None; top_water = None
    for wy in range(y_hi, y_lo - 1, -1):
        b = section_block(chunk, wy, lx, lz)
        if b in ("air", "void_air", "cave_air"):
            continue
        if top_water is None and b in WATER:
            top_water = wy
        if top_solid is None and b not in WATER:
            top_solid = wy
        if top_solid is not None:
            break
    return top_solid, top_water


def main(orient, mdir, ta, tb):
    # mask reads at the seam for classification
    lk = rasterio.open("masks/hydro_lake.tif")
    lkwl = rasterio.open("masks/hydro_lake_wl.tif")
    cl = rasterio.open("masks/hydro_centerline.tif")

    if orient == "H":      # north tile (ta,tb), south tile (ta,tb+1)
        nmca = f"{mdir}/r.{ta}.{tb}.mca"; smca = f"{mdir}/r.{ta}.{tb+1}.mca"
        nz_local, sz_local = 511, 0
        wz_n = tb * 512 + 511; wz_s = (tb + 1) * 512
        def cols():
            for lx in range(512):
                wx = ta * 512 + lx
                yield lx, wx, (nmca, lx, nz_local, wx, wz_n), (smca, lx, sz_local, wx, wz_s)
    else:                  # west tile (ta,tb), east tile (ta+1,tb)
        wmca = f"{mdir}/r.{ta}.{tb}.mca"; emca = f"{mdir}/r.{ta+1}.{tb}.mca"
        def cols():
            for lz in range(512):
                wz = tb * 512 + lz
                yield lz, wz, (wmca, 511, lz, ta*512+511, wz), (emca, 0, lz, (ta+1)*512, wz)

    cache = {}
    def get_chunk(mca, cx, cz):
        k = (mca, cx, cz)
        if k not in cache:
            cache[k] = read_chunk(mca, cx, cz)
        return cache[k]

    print(f"seam {orient} between r.{ta}.{tb} and "
          f"r.{ta}.{tb+1 if orient=='H' else tb}/r.{ta+1 if orient=='V' else ta}.{tb}")
    print(f"{'idx':>4} {'wx/wz':>6} | {'A_solid':>7} {'A_water':>7} | "
          f"{'B_solid':>7} {'B_water':>7} | {'dSolid':>6} {'dWater':>6} | type")
    big_water = []; big_solid = []
    for idx, wcoord, A, B in cols():
        (amca, alx, alz, awx, awz) = A; (bmca, blx, blz, bwx, bwz) = B
        ach = get_chunk(amca, alx >> 4, alz >> 4)
        bch = get_chunk(bmca, blx >> 4, blz >> 4)
        if ach is None or bch is None:
            continue
        as_, aw = col_scan(ach, alx & 15, alz & 15)
        bs_, bw = col_scan(bch, blx & 15, blz & 15)
        # classify by mask at the A side
        lake_id = int(lk.read(1, window=((awz, awz+1), (awx, awx+1)))[0, 0])
        lwl = float(lkwl.read(1, window=((awz, awz+1), (awx, awx+1)))[0, 0])
        riv = int(cl.read(1, window=((awz, awz+1), (awx, awx+1)))[0, 0])
        typ = "LAKE" if lake_id > 0 else ("river" if riv > 0 else "land")
        dS = (as_ - bs_) if (as_ is not None and bs_ is not None) else None
        dW = (aw - bw) if (aw is not None and bw is not None) else None
        flag = ""
        if dW is not None and abs(dW) >= 1:
            flag += " <WATER-STEP"; big_water.append((idx, typ, aw, bw, lwl))
        if dS is not None and abs(dS) >= 2:
            flag += " <SOLID-STEP"; big_solid.append((idx, typ, as_, bs_))
        if flag or typ != "land":
            print(f"{idx:>4} {wcoord:>6} | {str(as_):>7} {str(aw):>7} | "
                  f"{str(bs_):>7} {str(bw):>7} | {str(dS):>6} {str(dW):>6} | {typ}{flag}")
    print(f"\nSUMMARY: water-steps>=1: {len(big_water)}   solid-steps>=2: {len(big_solid)}")
    if big_water:
        lwls = set(round(x[4], 1) for x in big_water)
        print(f"  lake water-level(s) at seam (hydro_lake_wl): {sorted(lwls)}")
        print(f"  water-step columns: {[(i,t,a,b) for i,t,a,b,_ in big_water[:20]]}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
