"""Dump topmost solid block + first non-air-above per column for sample chunks.
Usage: py tools/diag_mca_surface.py <path/to/r.X.Z.mca>
"""
import struct, zlib, gzip, io, sys
from collections import Counter
import nbtlib


def read_chunk(mca_path: str, lx: int, lz: int):
    with open(mca_path, "rb") as f:
        idx = (lz % 32) * 32 + (lx % 32)
        f.seek(idx * 4)
        loc = f.read(4)
        offset = (loc[0] << 16) | (loc[1] << 8) | loc[2]
        sectors = loc[3]
        if offset == 0 or sectors == 0:
            return None
        f.seek(offset * 4096)
        length = struct.unpack(">I", f.read(4))[0]
        comp_type = f.read(1)[0]
        data = f.read(length - 1)
    if comp_type == 1:
        raw = gzip.decompress(data)
    elif comp_type == 2:
        raw = zlib.decompress(data)
    elif comp_type == 3:
        raw = data
    else:
        raise ValueError(f"Unknown compression type {comp_type}")
    return nbtlib.File.parse(io.BytesIO(raw))


def unpack_section(section):
    """Return 16x16x16 array of block-name strings.  None for air-only."""
    bs = section.get("block_states") if "block_states" in section else section.get("BlockStates")
    if bs is None:
        return None
    palette = bs.get("palette")
    data    = bs.get("data")
    if palette is None:
        return None
    pal_names = []
    for ent in palette:
        nm = str(ent.get("Name"))
        pal_names.append(nm)
    if data is None or len(palette) == 1:
        # All-same palette block (often air)
        return [[[pal_names[0]] * 16 for _ in range(16)] for __ in range(16)]
    # Unpack 4096-block longs
    bits_per = max(4, (len(palette) - 1).bit_length())
    longs = list(int(x) for x in data)
    out = [[[None] * 16 for _ in range(16)] for __ in range(16)]
    blocks_per_long = 64 // bits_per
    mask = (1 << bits_per) - 1
    block_idx = 0
    for L in longs:
        for _ in range(blocks_per_long):
            if block_idx >= 4096:
                break
            pi = L & mask
            L >>= bits_per
            yy = block_idx // 256
            zz = (block_idx // 16) % 16
            xx = block_idx % 16
            out[yy][zz][xx] = pal_names[pi]
            block_idx += 1
    return out


def dump_surface(mca_path: str, sample_chunks: int = 5):
    print(f"== {mca_path} ==")
    surface_blocks_seen: Counter = Counter()
    above_surface_seen: Counter = Counter()
    for i in range(sample_chunks):
        lx = (i * 6) % 32
        lz = (i * 6) % 32
        try:
            chunk = read_chunk(mca_path, lx, lz)
        except Exception as e:
            print(f"  chunk ({lx},{lz}): error {e}")
            continue
        if chunk is None:
            continue
        sections = chunk.get("sections") or chunk.root.get("sections")
        if sections is None:
            continue

        # Build dict: section_y → blocks_arr
        section_arrays = {}
        for sec in sections:
            sy = int(sec.get("Y", 0))
            arr = unpack_section(sec)
            if arr is not None:
                section_arrays[sy] = arr

        # For sample columns, find topmost solid + GC block above
        sample_cols = [(2, 2), (8, 8), (12, 4), (4, 12), (8, 13)]
        for cz, cx in sample_cols:
            top_solid = None
            for sec_y in sorted(section_arrays.keys(), reverse=True):
                arr = section_arrays[sec_y]
                for ly in range(15, -1, -1):
                    block = arr[ly][cz][cx]
                    if block is None:
                        continue
                    if "air" in block:
                        continue
                    top_solid = (sec_y * 16 + ly, block)
                    break
                if top_solid is not None:
                    break
            if top_solid is None:
                continue
            top_y, top_blk = top_solid
            surface_blocks_seen[top_blk.replace("minecraft:", "")] += 1
            # Check block at top_y+1
            above_y = top_y + 1
            above_sy = above_y // 16
            above_ly = above_y % 16
            if above_sy in section_arrays:
                above_blk = section_arrays[above_sy][above_ly][cz][cx]
                if above_blk and "air" not in above_blk:
                    above_surface_seen[above_blk.replace("minecraft:", "").split("[")[0]] += 1
                else:
                    above_surface_seen["(air)"] += 1
            print(f"  chunk({lx},{lz}) col({cz},{cx}) sy={top_y:3d} top={top_blk.replace('minecraft:', '')[:30]:<30}  above={above_blk.replace('minecraft:','')[:30] if above_blk else 'none'}")
    print()
    print(f"  Surface blocks: {dict(surface_blocks_seen.most_common())}")
    print(f"  GC blocks above: {dict(above_surface_seen.most_common())}")


if __name__ == "__main__":
    paths = sys.argv[1:] or ["output/r.32.13.mca"]
    for p in paths:
        try:
            dump_surface(p)
        except FileNotFoundError:
            print(f"  MISSING: {p}")
        print()
