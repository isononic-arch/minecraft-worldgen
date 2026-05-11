"""
diag_topblock_at_painted.py — read MCA, count topmost block at each painted
cell to find what's covering the water in topdown.
"""
from __future__ import annotations
import sys, struct, zlib, gzip, io
from pathlib import Path
import numpy as np
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nbtlib
from PIL import Image

mca_path = Path("C:/Users/nicho/minecraft-worldgen/.claude/worktrees/mystifying-buck-87c8c6/output/r.51.53.mca")
masks_dir = Path("C:/Users/nicho/minecraft-worldgen/masks")
TILE = 512
tx, tz = 51, 53

# 1) Compute the painted+smoothed mask at 50k for tile (51,53)
from core import tile_streamer
from core.hydro_region_overlay import apply_hydro_region_overlay
masks = tile_streamer.read_tile(masks_dir=masks_dir, col_off=tx*TILE,
                                  row_off=tz*TILE, width=TILE, height=TILE)
apply_hydro_region_overlay(masks, masks_dir, tx*TILE, tz*TILE, TILE)
painted_50k = masks["hydro_centerline"] > 0
print(f"painted cells in (51,53): {int(painted_50k.sum())}")

# 2) Read the MCA and build top-block array per (z,x) -- 512x512
def read_chunk(cx, cz):
    with open(mca_path, 'rb') as f:
        f.seek(4 * (cx + cz * 32))
        loc = f.read(4)
    offset = (loc[0] << 16) | (loc[1] << 8) | loc[2]
    sectors = loc[3]
    if offset == 0 or sectors == 0:
        return None
    with open(mca_path, 'rb') as f:
        f.seek(offset * 4096)
        length = struct.unpack('>I', f.read(4))[0]
        comp = f.read(1)[0]
        data = f.read(length - 1)
    if comp == 2:
        data = zlib.decompress(data)
    elif comp == 1:
        data = gzip.decompress(data)
    return nbtlib.File.parse(io.BytesIO(data))

def unpack_section(sec):
    bs = sec.get("block_states")
    if bs is None: return None
    pal = bs.get("palette", [])
    if not pal: return None
    names = [str(p["Name"]) for p in pal]
    d = bs.get("data")
    if d is None:
        # Single-palette section: all 4096 cells = palette[0]
        out = [[ [names[0]]*16 for _ in range(16)] for _ in range(16)]
        return out
    longs = list(d)
    n_pal = len(names)
    bits = max(4, (n_pal-1).bit_length())
    blocks_per_long = 64 // bits
    mask = (1 << bits) - 1
    out = [[ [None]*16 for _ in range(16)] for _ in range(16)]
    bi = 0
    for L in longs:
        Lv = L & 0xFFFFFFFFFFFFFFFF
        for _ in range(blocks_per_long):
            if bi >= 4096: break
            pi = Lv & mask
            Lv >>= bits
            yy = bi // 256; zz = (bi // 16) % 16; xx = bi % 16
            out[yy][zz][xx] = names[pi]
            bi += 1
        if bi >= 4096: break
    return out

top_blocks = np.full((512, 512), "?", dtype=object)
for cz in range(32):
    for cx in range(32):
        chunk = read_chunk(cx, cz)
        if chunk is None: continue
        sections = chunk.get("sections")
        if sections is None: continue
        sec_arrays = {}
        for sec in sections:
            sy = int(sec.get("Y", 0))
            arr = unpack_section(sec)
            if arr is not None:
                sec_arrays[sy] = arr
        for lz in range(16):
            for lx in range(16):
                top_block = "air"
                for sy in sorted(sec_arrays.keys(), reverse=True):
                    arr = sec_arrays[sy]
                    for ly in range(15, -1, -1):
                        b = arr[ly][lz][lx]
                        if b is None: continue
                        bs = b.replace("minecraft:", "").split("[")[0]
                        if bs != "air":
                            top_block = bs
                            break
                    if top_block != "air":
                        break
                top_blocks[cz*16 + lz, cx*16 + lx] = top_block
    if cz % 8 == 7:
        print(f"  parsed chunks up to row {cz+1}")

# 3) Tally topmost block at painted cells
from collections import Counter
painted_top = top_blocks[painted_50k]
counts = Counter(painted_top.tolist())
print(f"\nTop-block distribution at {len(painted_top)} painted cells:")
for n, c in counts.most_common(20):
    print(f"  {n:30s} {c:6d} ({100*c/len(painted_top):5.1f}%)")
