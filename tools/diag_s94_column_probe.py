"""S94 column probe: at a user-reported 'column still there' coord, read the
ACTUAL installed MCA block stack and cross-check against the Step-9 dump arrays
(sy_final / river_water_y / river_meta). Answers: did despike's lowering reach
the blocks, and what is chunk_writer building the emergent column from?

Usage: py tools/diag_s94_column_probe.py <mca> <dump_dir> <tx> <tz> <lz> <lx>
"""
import sys, io, zlib, struct
import numpy as np
import nbtlib


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
    sec_y = wy >> 4
    ly = wy & 15
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
        bits = max(4, (len(names) - 1).bit_length())
        per = 64 // bits
        bi = (ly * 256) + (lz * 16) + lx
        L = int(data[bi // per]); shift = (bi % per) * bits
        return names[(L >> shift) & ((1 << bits) - 1)]
    return "air"


def col_top(chunk, lx, lz, y_hi=200, y_lo=55):
    """Highest non-air block Y in [y_lo, y_hi]."""
    for wy in range(y_hi, y_lo - 1, -1):
        b = section_block(chunk, wy, lx, lz)
        if b not in ("air", "void_air", "cave_air"):
            return wy, b
    return None, "air"


def main(mca, dump, tx, tz, lz, lx):
    sy = wy = rm = None
    try:
        sy = np.load(f"{dump}/sy_final_{tx}_{tz}.npy").astype(np.int32)
        wy = np.load(f"{dump}/rwy_{tx}_{tz}.npy").astype(np.int32)
        rm = np.load(f"{dump}/rmeta9_{tx}_{tz}.npy")
        print(f"dump loaded: sy{sy.shape} rwy{wy.shape} rm{rm.shape}")
    except Exception as e:
        print(f"(no dump: {e})")

    print(f"\n=== probe tile ({tx},{tz}) local (z={lz}, x={lx}) "
          f"world ({tx*512+lx}, {tz*512+lz}) ===\n")
    R = 3
    for z in range(lz - R, lz + R + 1):
        for x in range(lx - R, lx + R + 1):
            cx, cz = x >> 4, z >> 4
            ch = read_chunk(mca, cx, cz)
            tag = "<<" if (z == lz and x == lx) else "  "
            if ch is None:
                print(f"{tag}({z},{x}) chunk missing")
                continue
            top_y, top_b = col_top(ch, x & 15, z & 15)
            dvals = ""
            if sy is not None:
                dvals = (f"  dump: sy={sy[z,x]} rwy={wy[z,x]} "
                         f"rm={int(rm[z,x])} emerg={sy[z,x]>=wy[z,x] and wy[z,x]>63}")
            print(f"{tag}({z},{x}) MCA top: Y={top_y} {top_b}{dvals}")
        print()

    # full vertical stack at the exact column
    cx, cz = lx >> 4, lz >> 4
    ch = read_chunk(mca, cx, cz)
    if ch is not None:
        print(f"--- full stack at ({lz},{lx}) from Y=80 down to 58 ---")
        for wyv in range(80, 57, -1):
            b = section_block(ch, wyv, lx & 15, lz & 15)
            mark = ""
            if wy is not None and wyv == wy[lz, lx]:
                mark = "  <- river_water_y"
            if sy is not None and wyv == sy[lz, lx]:
                mark += "  <- dump sy_final"
            print(f"   Y={wyv:3d} {b}{mark}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]),
         int(sys.argv[5]), int(sys.argv[6]))
