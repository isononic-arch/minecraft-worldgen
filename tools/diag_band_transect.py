"""Read topmost solid block along a world-X transect at fixed world-Z, from a region MCA.
Usage: py tools/diag_band_transect.py <r.X.Z.mca> <world_x0> <world_x1> <world_z>
Prints per-column: world_x, surface_y, block.  Groups runs of identical block.
"""
import struct, zlib, gzip, io, sys
from collections import Counter
import nbtlib

sys.path.insert(0, "tools")
from diag_mca_surface import read_chunk, unpack_section


def top_solid_at(section_arrays, ly_cx, ly_cz):
    for sec_y in sorted(section_arrays.keys(), reverse=True):
        arr = section_arrays[sec_y]
        for ly in range(15, -1, -1):
            block = arr[ly][ly_cz][ly_cx]
            if block is None or "air" in block:
                continue
            return (sec_y * 16 + ly, block.replace("minecraft:", "").split("[")[0])
    return (None, None)


def transect(mca_path, wx0, wx1, wz):
    rx = wx0 // 512
    rz = wz // 512
    chunk_cache = {}
    rows = []
    for wx in range(wx0, wx1 + 1):
        lx_block = wx - rx * 512
        lz_block = wz - rz * 512
        cx, cz = lx_block // 16, lz_block // 16
        if (cx, cz) not in chunk_cache:
            try:
                ch = read_chunk(mca_path, cx, cz)
            except Exception as e:
                ch = None
            sa = {}
            if ch is not None:
                sections = ch.get("sections") or ch.root.get("sections")
                if sections:
                    for sec in sections:
                        arr = unpack_section(sec)
                        if arr is not None:
                            sa[int(sec.get("Y", 0))] = arr
            chunk_cache[(cx, cz)] = sa
        sa = chunk_cache[(cx, cz)]
        sy, blk = top_solid_at(sa, lx_block % 16, lz_block % 16)
        rows.append((wx, sy, blk))
    # print run-length grouped
    print(f"== transect z={wz}, x {wx0}..{wx1} on {mca_path} ==")
    prev = None
    start = None
    counts = Counter()
    for wx, sy, blk in rows:
        counts[blk] += 1
        if blk != prev:
            if prev is not None:
                print(f"  x {start:>6}..{wx-1:<6} ({wx-start:>3} wide)  y~{psy:<4} {prev}")
            prev, start, psy = blk, wx, sy
    if prev is not None:
        print(f"  x {start:>6}..{rows[-1][0]:<6} ({rows[-1][0]-start+1:>3} wide)  y~{psy:<4} {prev}")
    print(f"  totals: {dict(counts.most_common())}")


if __name__ == "__main__":
    p = sys.argv[1]
    wx0, wx1, wz = int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    transect(p, wx0, wx1, wz)
