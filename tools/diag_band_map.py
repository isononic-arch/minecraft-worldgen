"""2D top-block map of an MCA region patch, as a legend grid.
Usage: py tools/diag_band_map.py <r.X.Z.mca> <wx0> <wx1> <wz0> <wz1>
"""
import sys
from collections import Counter
sys.path.insert(0, "tools")
from diag_mca_surface import read_chunk, unpack_section


def top_solid(sa, lx, lz):
    for sy in sorted(sa.keys(), reverse=True):
        arr = sa[sy]
        for ly in range(15, -1, -1):
            b = arr[ly][lz][lx]
            if b is None or "air" in b:
                continue
            return b.replace("minecraft:", "").split("[")[0]
    return None


# block -> single-char legend (forest = lowercase/space-ish, rock/wash = UPPER)
LEG = {
    "grass_block": ".", "short_grass": ".", "tall_grass": ".", "fern": ".",
    "podzol": ",", "dirt": ",", "coarse_dirt": "d", "rooted_dirt": "d",
    "oak_leaves": "t", "spruce_leaves": "t", "birch_leaves": "t", "dark_oak_leaves": "t",
    "oak_log": "|", "spruce_log": "|", "birch_log": "|",
    "calcite": "C", "white_concrete_powder": "W", "light_gray_concrete_powder": "L",
    "diorite": "D", "cobblestone": "B", "andesite": "A", "stone": "S", "gravel": "G",
    "dripstone_block": "p", "packed_mud": "m", "mud": "m", "clay": "y", "sand": "s",
    "tuff": "f", "deepslate": "x", "basalt": "z",
}


def main(p, wx0, wx1, wz0, wz1):
    rx, rz = wx0 // 512, wz0 // 512
    cache = {}
    counts = Counter()
    print(f"== block map {p}  x[{wx0}..{wx1}] z[{wz0}..{wz1}] ==")
    hdr = "      " + "".join(str((x // 10) % 10) for x in range(wx0, wx1 + 1))
    print(hdr)
    for wz in range(wz0, wz1 + 1):
        row = []
        for wx in range(wx0, wx1 + 1):
            lxb, lzb = wx - rx * 512, wz - rz * 512
            ck = (lxb // 16, lzb // 16)
            if ck not in cache:
                try:
                    ch = read_chunk(p, ck[0], ck[1])
                except Exception:
                    ch = None
                sa = {}
                if ch is not None:
                    secs = ch.get("sections") or ch.root.get("sections")
                    if secs:
                        for sec in secs:
                            arr = unpack_section(sec)
                            if arr is not None:
                                sa[int(sec.get("Y", 0))] = arr
                cache[ck] = sa
            b = top_solid(cache[ck], lxb % 16, lzb % 16)
            counts[b] += 1
            row.append(LEG.get(b, "?") if b else " ")
        print(f"z{wz} {''.join(row)}")
    print("\nlegend: .=grass ,=podzol/dirt d=coarsedirt t=leaves |=log  "
          "C=calcite W=whiteCP L=lgrayCP D=diorite B=cobble A=andesite S=stone "
          "G=gravel p=dripstone m=mud/packedmud y=clay s=sand f=tuff x=deepslate z=basalt ?=other")
    print("counts:", dict(counts.most_common(20)))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5]))
