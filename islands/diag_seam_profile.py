"""diag_seam_profile.py — for one X region seam, profile the per-row Y step vs
the local elevation + distance-to-water, from the FINAL rendered MCAs. Tells us
whether the big-step seam cells are near-water (land-lock water-zone exclusion)
or interior mid-elevation rock (crunch-mask asymmetry)."""
import sys, struct, zlib, gzip, io, json
from pathlib import Path
import numpy as np
import nbtlib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ISL = ROOT / "islands"


def read_chunk(f, lx, lz):
    idx = (lz % 32) * 32 + (lx % 32)
    f.seek(idx * 4); loc = f.read(4)
    off = (loc[0] << 16) | (loc[1] << 8) | loc[2]; secs = loc[3]
    if off == 0 or secs == 0:
        return None
    f.seek(off * 4096); length = struct.unpack(">I", f.read(4))[0]
    ct = f.read(1)[0]; data = f.read(length - 1)
    raw = gzip.decompress(data) if ct == 1 else zlib.decompress(data) if ct == 2 else data
    return nbtlib.File.parse(io.BytesIO(raw))


def section_solid(sec):
    bs = sec.get("block_states");  pal = bs.get("palette") if bs else None
    if pal is None:
        return None
    names = [str(p.get("Name", "minecraft:air")) for p in pal]
    nonsolid = np.array([n in ("minecraft:air", "minecraft:cave_air",
                               "minecraft:void_air", "minecraft:water") for n in names], bool)
    if len(names) == 1:
        return np.full((16, 16, 16), not nonsolid[0])
    data = bs.get("data")
    if data is None:
        return np.full((16, 16, 16), not nonsolid[0])
    bits = max(4, (len(names) - 1).bit_length()); per = 64 // bits
    mask = np.uint64((1 << bits) - 1)
    longs = np.asarray(data, dtype=np.uint64)
    shifts = np.arange(per, dtype=np.uint64) * np.uint64(bits)
    idx = ((longs[:, None] >> shifts[None, :]) & mask).reshape(-1)[:4096].astype(np.int64)
    return (~nonsolid[np.clip(idx, 0, len(names) - 1)]).reshape(16, 16, 16)


def chunk_topY(chunk):
    items = []
    for s in (chunk.get("sections") or []):
        sb = section_solid(s)
        if sb is not None:
            items.append((int(s.get("Y", 0)), sb))
    topY = np.full((16, 16), -999, np.int32); found = np.zeros((16, 16), bool)
    for sy, sb in sorted(items, key=lambda t: t[0], reverse=True):
        for yy in range(15, -1, -1):
            fill = (~found) & sb[yy]
            if fill.any():
                topY[fill] = sy * 16 + yy; found |= fill
        if found.all():
            break
    return topY


def col_region_map(odir, region_x_list, rz0, rz1):
    """Build top-Y for a vertical strip of regions across z range. Returns dict
    keyed (rx) -> array (rows, 512) world-x indexed, plus z origin."""
    bz = rz0 * 512
    H = (rz1 - rz0 + 1) * 512
    out = {}
    for rx in region_x_list:
        Y = np.full((H, 512), -999, np.int32)
        for rz in range(rz0, rz1 + 1):
            mca = odir / f"r.{rx}.{rz}.mca"
            if not mca.exists():
                continue
            with open(mca, "rb") as f:
                for lz in range(32):
                    for lx in range(32):
                        try:
                            ch = read_chunk(f, lx, lz)
                        except Exception:
                            ch = None
                        if ch is None:
                            continue
                        tY = chunk_topY(ch)
                        cz = (rz * 32 + lz) * 16 - bz; cx = lx * 16
                        if 0 <= cz < H:
                            Y[cz:cz+16, cx:cx+16] = tY
        out[rx] = Y
    return out, bz


def main():
    name = "new_vincentia_st_kitts_nevis_statia"
    odir = ISL / "out" / name
    # x=5120 seam = boundary between region 9 (cols ...511) and region 10 (col 0)
    rzs = list(range(15, 31))
    strip, bz = col_region_map(odir, [9, 10], rzs[0], rzs[-1])
    A = strip[9][:, -1]      # region 9 last col (world x 5119)
    B = strip[10][:, 0]      # region 10 first col (world x 5120)
    # local water-proximity proxy: min over a window of how close Y gets to <=63
    step = np.abs(A.astype(int) - B.astype(int))
    land = (A > 63) & (B > 63)
    # distance to nearest <=63 along the seam column (1D proxy)
    sub63 = (A <= 63) | (B <= 63)
    idx = np.arange(len(A))
    water_idx = idx[sub63]
    if len(water_idx):
        dist_water = np.min(np.abs(idx[:, None] - water_idx[None, :]), axis=1)
    else:
        dist_water = np.full(len(A), 9999)
    big = land & (step >= 3)
    print(f"seam x=5120  land cells={int(land.sum())}  big-step(>=3)={int(big.sum())}")
    if big.any():
        elev = (A[big] + B[big]) / 2.0
        dw = dist_water[big]
        print(f"  big-step cells: elevation mean={elev.mean():.0f} min={elev.min():.0f} max={elev.max():.0f}")
        print(f"  big-step cells: dist-to-water(<=63 along seam) mean={dw.mean():.0f} min={int(dw.min())} max={int(dw.max())}")
        print(f"  big-step cells within 14 of water: {int((dw<=14).sum())}/{int(big.sum())}")
        print(f"  big-step cells within 30 of water: {int((dw<=30).sum())}/{int(big.sum())}")
    if land.any():
        print(f"  ALL land cells: elevation mean={((A[land]+B[land])/2).mean():.0f}")
        print(f"  ALL land cells dist-water mean={dist_water[land].mean():.0f}")
    # show a few big-step rows
    rows = idx[big][:12]
    print("  sample big rows (z, A, B, step, dist_water):")
    for r in rows:
        print(f"    z={bz+r} A={A[r]} B={B[r]} step={step[r]} dw={int(dist_water[r])}")


if __name__ == "__main__":
    main()
