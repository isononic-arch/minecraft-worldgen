"""Quick diagnostic: dump biome NBT cells from a chunk in an .mca region file.
Usage: py tools/diag_mca_biomes.py <path/to/r.X.Z.mca>
"""
import struct, zlib, gzip, io, sys
from collections import Counter
import nbtlib


def read_chunk(mca_path: str, lx: int, lz: int):
    """Read chunk at local (x, z) within region.  Returns NBT compound or None."""
    with open(mca_path, "rb") as f:
        # Location table: 1024 entries, each 4 bytes (3 offset + 1 sector count)
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


def dump_biomes(mca_path: str, sample_chunks: int = 8):
    """Dump unique biomes across N sampled chunks in this region."""
    print(f"== {mca_path} ==")
    all_biomes: Counter = Counter()
    for i in range(sample_chunks):
        lx = (i * 4) % 32
        lz = (i * 4) % 32
        try:
            chunk = read_chunk(mca_path, lx, lz)
        except Exception as e:
            print(f"  chunk ({lx},{lz}): error {e}")
            continue
        if chunk is None:
            continue
        # nbtlib loads File which is a Compound — root tag at File.root or directly
        sections = chunk.get("sections") or chunk.root.get("sections")
        if sections is None:
            print(f"  chunk ({lx},{lz}): no sections")
            continue
        for sec_idx, sec in enumerate(sections):
            biomes = sec.get("biomes")
            if biomes is None:
                continue
            palette = biomes.get("palette")
            if palette is None:
                continue
            for entry in palette:
                all_biomes[str(entry)] += 1
    print(f"  Unique biomes across {sample_chunks} chunks (palette occurrences):")
    for biome, count in all_biomes.most_common():
        print(f"    {count:4d}x  {biome}")


if __name__ == "__main__":
    paths = sys.argv[1:] or [
        "output/r.37.8.mca",
        "output/r.59.44.mca",
    ]
    for p in paths:
        try:
            dump_biomes(p)
        except FileNotFoundError:
            print(f"  MISSING: {p}")
        print()
