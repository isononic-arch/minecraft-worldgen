"""S94 (B) verify: read the actual MCA blocks at river-bank step risers and
confirm the vertical face shows DIRT, not the stone basement.
Usage: py tools/diag_verify_dirt_risers.py <dump_dir> <out_dir> <tx> <tz>
"""
import sys, io, zlib, struct
import numpy as np
import nbtlib

STONEY = ("stone", "cobble", "andesite", "diorite", "granite", "deepslate",
          "tuff", "calcite", "basalt", "dripstone", "concrete", "gravel",
          "sandstone", "blackstone", "coral")
SOILY = ("dirt", "grass", "mud", "podzol", "coarse", "rooted", "moss", "clay",
         "sand", "packed_mud", "farmland", "mycelium")


def read_chunk(mca_path, cx, cz):
    with open(mca_path, "rb") as f:
        header = f.read(4096)
        idx = (cx & 31) + (cz & 31) * 32
        off, n = struct.unpack(">IB", b"\x00" + header[idx*4:idx*4+3] + header[idx*4+3:idx*4+4])
        if off == 0:
            return None
        f.seek(off * 4096)
        ln = struct.unpack(">I", f.read(4))[0]
        ctype = f.read(1)[0]
        raw = f.read(ln - 1)
        raw = zlib.decompress(raw) if ctype == 2 else raw
    return nbtlib.File.parse(io.BytesIO(raw))


def section_block(chunk, wy, lx, lz):
    """Return block name at world Y wy, local (lx,lz) in chunk."""
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


def main(dump, out, tx, tz):
    sy = np.load(f"{dump}/sy_final_{tx}_{tz}.npy").astype(np.int32)
    wy = np.load(f"{dump}/rwy_{tx}_{tz}.npy").astype(np.int32)
    rm = np.load(f"{dump}/rmeta9_{tx}_{tz}.npy")
    river = (rm == 1) | (rm == 2); wet = river & (wy > 63) & (wy > sy)
    land = ~river & (rm != 3) & (sy > 63)
    from scipy.ndimage import distance_transform_edt
    dist = distance_transform_edt(~wet)
    # find bank land cells with a >=2 step DOWN to a lower land neighbour, near river
    cand = []
    for z in range(1, 511):
        for x in range(1, 511):
            if not (land[z, x] and dist[z, x] <= 12):
                continue
            lows = [sy[z+dz, x+dx] for dz, dx in ((1,0),(-1,0),(0,1),(0,-1))
                    if land[z+dz, x+dx]]
            if lows and sy[z, x] - min(lows) >= 2:
                cand.append((z, x, sy[z, x], min(lows)))
    print(f"({tx},{tz}) bank riser cells (>=2 step, near river): {len(cand)}")
    mca = f"{out}/r.{tx}.{tz}.mca"
    stone_faces = soil_faces = 0
    shown = 0
    for (z, x, top, low) in cand[:400]:
        cx, cz = x >> 4, z >> 4
        ch = read_chunk(mca, cx, cz)
        if ch is None:
            continue
        # the riser FACE = blocks from low+1 .. top at this column
        face = [section_block(ch, yy, x & 15, z & 15) for yy in range(low + 1, top + 1)]
        st = any(any(s in b for s in STONEY) for b in face)
        so = all(any(s in b for s in SOILY) or b == "air" for b in face)
        stone_faces += st; soil_faces += so
        if shown < 6:
            print(f"  ({z},{x}) top={top} low={low} face[{low+1}..{top}]={face}")
            shown += 1
    print(f"  riser faces sampled: stone-bearing={stone_faces}  all-soil={soil_faces}")
    print(f"  => B working if all-soil >> stone-bearing")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
