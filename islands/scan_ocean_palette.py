"""scan_ocean_palette.py — sample ocean columns from a rendered world and tally
the OCEAN-FLOOR SURFACE block (the block directly beneath the lowest water block)
plus the next two below, to learn the surface-decorator ocean palette to match in
the noise generator. No rendering — reads existing .mca only.
Usage: py islands/scan_ocean_palette.py --world "<path>" [--regions 12] [--step 4]
"""
import sys, argparse, glob, os, re
from collections import Counter
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from islands.topdown_mca import read_chunk, section_blocks

SEA = 63


def col_at(ch, bx, bz):
    secs = ch.get("sections") or ch.get("Sections") or []
    col = {}
    for s in secs:
        y = int(s.get("Y", 0)); b = section_blocks(s)
        if b is None:
            continue
        for yy in range(16):
            col[y * 16 + yy] = str(b[yy, bz, bx]).replace("minecraft:", "")
    return col


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True)
    ap.add_argument("--regions", type=int, default=16)
    ap.add_argument("--step", type=int, default=4)
    a = ap.parse_args()
    files = sorted(glob.glob(os.path.join(a.world, "region", "*.mca")))
    surf = Counter(); sub1 = Counter(); sub2 = Counter()
    depths = []; n_ocean = 0; n_cols = 0
    for mca in files[:a.regions]:
        try:
            f = open(mca, "rb")
        except Exception:
            continue
        with f:
            for lz in range(0, 32, a.step):
                for lx in range(0, 32, a.step):
                    try:
                        ch = read_chunk(f, lx, lz)
                    except Exception:
                        ch = None
                    if ch is None:
                        continue
                    n_cols += 1
                    col = col_at(ch, 8, 8)
                    # ocean column = water at/near sea level and air above
                    if col.get(SEA) != "water" or col.get(SEA + 2, "air") != "air":
                        continue
                    # find lowest contiguous water from sea level down
                    y = SEA
                    while col.get(y) == "water":
                        y -= 1
                    floor = col.get(y, "?")
                    if floor in ("?", "air", "water"):
                        continue
                    n_ocean += 1
                    depths.append(SEA - y)
                    surf[floor] += 1
                    sub1[col.get(y - 1, "?")] += 1
                    sub2[col.get(y - 2, "?")] += 1
    print(f"scanned {n_cols} cols across {min(len(files),a.regions)} regions -> {n_ocean} ocean cols")
    if n_ocean:
        d = np.array(depths)
        print(f"water depth (sea63 - floor): mean {d.mean():.0f}  median {int(np.median(d))}  "
              f"min {d.min()} max {d.max()}  -> floor top Y ~{SEA-int(np.median(d))}")
    def show(name, c):
        tot = sum(c.values()) or 1
        print(f"  {name:16}", ", ".join(f"{k} {v/tot*100:.0f}%" for k, v in c.most_common(6)))
    print("OCEAN-FLOOR SURFACE palette:")
    show("floor top", surf)
    show("1 below", sub1)
    show("2 below", sub2)


if __name__ == "__main__":
    main()
