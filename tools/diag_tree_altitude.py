"""diag_tree_altitude.py — count tree presence per surface-altitude band.

For each column: find top solid surface Y, and whether the column contains a
log/wood block (a tree trunk).  Bin tree-columns and total-land-columns by
altitude so we can see tree DENSITY (trees / land cols) vs altitude.

Use to verify krummholz: density should be ~normal below feather_lo, then drop
sharply and fade to ~0 toward the treeline top.

Usage: py tools/diag_tree_altitude.py <a.mca> [b.mca ...] [--bands 450,500,550,600,650,700]
"""
import struct, zlib, gzip, io, sys
import numpy as np
import nbtlib

Y_MIN, Y_LEVELS = -64, 768


def read_chunk(mca_path, lx, lz):
    with open(mca_path, "rb") as f:
        idx = (lz % 32) * 32 + (lx % 32)
        f.seek(idx * 4)
        loc = f.read(4)
        offset = (loc[0] << 16) | (loc[1] << 8) | loc[2]
        if offset == 0 or loc[3] == 0:
            return None
        f.seek(offset * 4096)
        length = struct.unpack(">I", f.read(4))[0]
        ct = f.read(1)[0]
        data = f.read(length - 1)
    raw = gzip.decompress(data) if ct == 1 else zlib.decompress(data) if ct == 2 else data
    return nbtlib.File.parse(io.BytesIO(raw))


def cat_section(section):
    """(16,16,16) [y][z][x] int8: 0 air, 1 log/wood, 2 other-solid."""
    bs = section.get("block_states") or section.get("BlockStates")
    if bs is None:
        return None
    pal = bs.get("palette")
    if pal is None:
        return None
    names = [str(e.get("Name")).replace("minecraft:", "").split("[")[0] for e in pal]
    lut = np.zeros(len(names), np.int8)
    for i, nm in enumerate(names):
        if nm == "air" or nm == "cave_air" or nm == "void_air":
            lut[i] = 0
        elif nm.endswith("_log") or nm.endswith("_wood") or nm.endswith("_stem") or nm.endswith("_hyphae"):
            lut[i] = 1
        else:
            lut[i] = 2
    data = bs.get("data")
    if data is None or len(pal) == 1:
        return np.full((16, 16, 16), lut[0], np.int8)
    bits = max(4, (len(pal) - 1).bit_length())
    longs = np.array([int(x) & 0xFFFFFFFFFFFFFFFF for x in data], np.uint64)
    bpl = 64 // bits
    mask = np.uint64((1 << bits) - 1)
    n_longs = len(longs)
    # 1.18+ padded format: block_idx = long_index*bpl + slot (li-major/slot-minor)
    vals = np.zeros((n_longs, bpl), dtype=np.int64)
    for slot in range(bpl):
        vals[:, slot] = ((longs >> np.uint64(slot * bits)) & mask).astype(np.int64)
    idxs = vals.reshape(-1)[:4096]
    return lut[idxs].reshape(16, 16, 16)


def analyze(mca):
    surf = np.full((512, 512), -9999, np.int32)
    haslog = np.zeros((512, 512), bool)
    logh = np.zeros((512, 512), np.int16)   # count of log blocks in the column (trunk height proxy)
    for cz in range(32):
        for cx in range(32):
            ch = read_chunk(mca, cx, cz)
            if ch is None:
                continue
            secs = ch.get("sections") or (ch.root.get("sections") if hasattr(ch, "root") else None)
            if not secs:
                continue
            col = np.zeros((Y_LEVELS, 16, 16), np.int8)
            for s in secs:
                sy = int(s.get("Y", 0)); base = sy * 16 - Y_MIN
                if 0 <= base <= Y_LEVELS - 16:
                    c = cat_section(s)
                    if c is not None:
                        col[base:base + 16] = c
            # for surface treat logs as non-ground: surface = top of cat==2
            ground = (col == 2)
            hg = ground.any(axis=0)
            ts = (Y_LEVELS - 1) - np.argmax(ground[::-1], axis=0)
            ts = np.where(hg, ts + Y_MIN, -9999)
            islog = (col == 1)
            lg = islog.any(axis=0)
            lh = islog.sum(axis=0).astype(np.int16)
            oz, ox = cz * 16, cx * 16
            surf[oz:oz + 16, ox:ox + 16] = ts
            haslog[oz:oz + 16, ox:ox + 16] = lg
            logh[oz:oz + 16, ox:ox + 16] = lh
    return surf, haslog, logh


def report(mca, bands):
    surf, haslog, logh = analyze(mca)
    land = surf > -9999
    print(f"\n== {mca} ==  land_cols={int(land.sum())} tree_cols={int((haslog&land).sum())}")
    edges = [-9999] + bands + [9999]
    print("  altitude band      land_cols   tree_cols   density%   mean_trunk  short(<=7)%")
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        m = land & (surf >= lo) & (surf < hi)
        n = int(m.sum())
        tm = m & haslog
        t = int(tm.sum())
        d = (100 * t / n) if n else 0
        hh = logh[tm]
        mh = float(hh.mean()) if hh.size else 0.0
        short = (100 * int((hh <= 7).sum()) / hh.size) if hh.size else 0.0
        lbl = f"{max(lo,-64):>4}..{min(hi,703):<4}"
        print(f"  {lbl:>12}      {n:>8}   {t:>8}   {d:6.2f}     {mh:6.1f}     {short:6.1f}")


if __name__ == "__main__":
    args = sys.argv[1:]
    bands = [450, 500, 550, 575, 600, 650, 700]
    paths = []
    i = 0
    while i < len(args):
        if args[i] == "--bands":
            bands = [int(x) for x in args[i + 1].split(",")]; i += 2
        else:
            paths.append(args[i]); i += 1
    for p in paths:
        try:
            report(p, bands)
        except FileNotFoundError:
            print(f"MISSING {p}")
