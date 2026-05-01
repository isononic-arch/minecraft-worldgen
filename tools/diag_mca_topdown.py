"""
diag_mca_topdown.py - Render a 512x512 top-down PNG of an MCA region file.

Walks every column in the region, finds the topmost non-air block, colors
it by block type.  Useful for previewing carved tiles without launching
Minecraft.

Usage:
    py tools/diag_mca_topdown.py path/to/r.51.53.mca [--out memory/foo.png]
"""
from __future__ import annotations

import argparse
import gzip
import io
import struct
import sys
import zlib
from pathlib import Path

import nbtlib
import numpy as np
from PIL import Image

# Block-name -> RGB.  Patterns matched in order; first match wins.
BLOCK_COLORS = [
    ("water",          (60, 110, 180)),
    ("grass_block",    (95, 145, 70)),
    ("dirt",           (105, 75, 55)),
    ("coarse_dirt",    (110, 75, 50)),
    ("podzol",         (90, 60, 40)),
    ("mud",            (75, 60, 50)),
    ("sand",           (220, 200, 140)),
    ("red_sand",       (200, 130, 60)),
    ("gravel",         (140, 135, 130)),
    ("stone",          (125, 125, 125)),
    ("andesite",       (135, 135, 135)),
    ("granite",        (165, 110, 90)),
    ("diorite",        (180, 175, 175)),
    ("calcite",        (220, 220, 215)),
    ("tuff",           (115, 115, 100)),
    ("deepslate",      (75, 75, 80)),
    ("snow_block",     (240, 240, 245)),
    ("snow",           (240, 240, 245)),
    ("ice",            (170, 200, 230)),
    ("packed_ice",     (160, 200, 230)),
    ("blue_ice",       (140, 200, 235)),
    ("powder_snow",    (235, 235, 240)),
    ("clay",           (155, 160, 165)),
    ("terracotta",     (155, 95, 60)),
    ("sandstone",      (215, 200, 145)),
    ("log",            (95, 70, 40)),     # *_log
    ("leaves",         (60, 110, 50)),    # *_leaves
    ("bush",           (75, 110, 60)),
    ("fern",           (85, 115, 65)),
    ("grass",          (95, 130, 65)),     # short/tall_grass etc
    ("flower",         (200, 150, 120)),   # generic flower fallback
    ("moss",           (75, 110, 55)),
    ("lily_pad",       (90, 130, 70)),
    ("kelp",           (50, 95, 55)),
    ("seagrass",       (75, 120, 80)),
    ("magma",          (180, 60, 30)),
    ("lava",           (220, 100, 30)),
    ("netherrack",     (130, 50, 50)),
]

DEFAULT_RGB = (200, 60, 200)  # magenta = unknown block (highlight if you see this)


def color_for(block_name: str) -> tuple[int, int, int]:
    if block_name is None:
        return (0, 0, 0)
    nm = block_name.replace("minecraft:", "").split("[")[0].lower()
    for pat, rgb in BLOCK_COLORS:
        if pat in nm:
            return rgb
    return DEFAULT_RGB


def read_chunk_nbt(mca_path: str, lx: int, lz: int):
    """Read chunk (lx, lz) from MCA region file.  Returns None if absent."""
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
        raise ValueError(f"unknown compression type {comp_type}")
    return nbtlib.File.parse(io.BytesIO(raw))


def unpack_section(section):
    """Return 16x16x16 list-of-list-of-list of block-name strings."""
    bs = section.get("block_states") if "block_states" in section else section.get("BlockStates")
    if bs is None:
        return None
    palette = bs.get("palette")
    data = bs.get("data")
    if palette is None:
        return None
    pal_names = [str(ent.get("Name")) for ent in palette]
    if data is None or len(palette) == 1:
        return [[[pal_names[0]] * 16 for _ in range(16)] for __ in range(16)]
    bits_per = max(4, (len(palette) - 1).bit_length())
    longs = [int(x) for x in data]
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


def render_region(mca_path: str) -> np.ndarray:
    """Render 512x512 RGB top-down for an MCA region."""
    img = np.zeros((512, 512, 3), dtype=np.uint8)
    height = np.full((512, 512), -64, dtype=np.int16)
    chunks_done = 0
    chunks_missing = 0
    for cz in range(32):
        for cx in range(32):
            try:
                chunk = read_chunk_nbt(mca_path, cx, cz)
            except Exception as e:
                print(f"  chunk ({cx},{cz}): error {e}", file=sys.stderr)
                continue
            if chunk is None:
                chunks_missing += 1
                continue
            chunks_done += 1
            sections = chunk.get("sections") or chunk.root.get("sections")
            if sections is None:
                continue
            section_arrays = {}
            for sec in sections:
                sy = int(sec.get("Y", 0))
                arr = unpack_section(sec)
                if arr is not None:
                    section_arrays[sy] = arr

            base_x = cx * 16
            base_z = cz * 16
            for lx in range(16):
                for lz in range(16):
                    top_block = None
                    top_y = -64
                    for sy in sorted(section_arrays.keys(), reverse=True):
                        arr = section_arrays[sy]
                        for ly in range(15, -1, -1):
                            block = arr[ly][lz][lx]
                            if block is None or "air" in block:
                                continue
                            top_block = block
                            top_y = sy * 16 + ly
                            break
                        if top_block is not None:
                            break
                    if top_block:
                        rgb = color_for(top_block)
                        # Apply altitude shading: brighter for high, darker for low
                        # Sea-level baseline at y=63
                        alt_factor = max(0.55, min(1.20, 0.85 + (top_y - 63) / 250.0))
                        r = int(min(255, rgb[0] * alt_factor))
                        g = int(min(255, rgb[1] * alt_factor))
                        b = int(min(255, rgb[2] * alt_factor))
                        img[base_z + lz, base_x + lx] = (r, g, b)
                        height[base_z + lz, base_x + lx] = top_y
        print(f"  row {cz+1}/32 done", end="\r", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  chunks: {chunks_done} rendered, {chunks_missing} missing",
          file=sys.stderr)
    return img


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mca", help="Path to .mca file")
    p.add_argument("--out", help="Output PNG path (default: <mca>.topdown.png)")
    args = p.parse_args()

    mca_path = Path(args.mca)
    if not mca_path.is_file():
        print(f"ERROR: {mca_path} not found", file=sys.stderr)
        return 1

    print(f"Rendering {mca_path}...", file=sys.stderr)
    img = render_region(str(mca_path))

    out = Path(args.out) if args.out else mca_path.with_suffix(".topdown.png")
    if not out.parent.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(str(out))
    print(f"Saved {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
