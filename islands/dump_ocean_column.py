"""dump_ocean_column.py — read the vertical block stack at world (x,z) from a
rendered world's region files, to learn the exact deep-ocean palette/depth.
Usage: py islands/dump_ocean_column.py --world "<path>" --x 500 --z 25000
"""
import sys, argparse, glob
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from islands.topdown_mca import read_chunk, section_blocks


def column(world, wx, wz):
    rx, rz = wx >> 9, wz >> 9
    mca = Path(world) / "region" / f"r.{rx}.{rz}.mca"
    if not mca.exists():
        return None, f"no region {mca.name}"
    lx, lz = (wx >> 4) & 31, (wz >> 4) & 31
    with open(mca, "rb") as f:
        ch = read_chunk(f, lx, lz)
    if ch is None:
        return None, "empty chunk"
    secs = ch.get("sections") or ch.get("Sections") or []
    bx, bz = wx & 15, wz & 15
    col = {}
    for s in secs:
        y = int(s.get("Y", 0)); b = section_blocks(s)
        if b is None:
            continue
        for yy in range(16):
            col[y * 16 + yy] = str(b[yy, bz, bx]).replace("minecraft:", "")
    return col, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True)
    ap.add_argument("--x", type=int, required=True)
    ap.add_argument("--z", type=int, required=True)
    ap.add_argument("--ylo", type=int, default=-64)
    ap.add_argument("--yhi", type=int, default=70)
    a = ap.parse_args()
    col, err = column(a.world, a.x, a.z)
    if err:
        print(f"({a.x},{a.z}): {err}"); return
    # compress runs
    prev = None; runs = []
    for y in range(a.yhi, a.ylo - 1, -1):
        b = col.get(y, "(none)")
        if b != prev:
            runs.append([y, y, b]); prev = b
        else:
            runs[-1][1] = y
    print(f"column ({a.x},{a.z})  Y{a.yhi}..{a.ylo}:")
    for hi, lo, b in runs:
        rng = f"Y{hi}" if hi == lo else f"Y{hi}..{lo}"
        print(f"  {rng:14} {b}")
    # summarize: sea surface, water bottom, floor top block
    ys = sorted(col)
    water_ys = [y for y in ys if col[y] == "water"]
    solid_below = [y for y in ys if col[y] not in ("water", "air", "(none)", "cave_air")]
    if water_ys:
        wb = min(water_ys)
        floor = col.get(wb - 1, "?")
        print(f"  -> water {max(water_ys)}..{wb} ({len(water_ys)} deep), floor top = {floor} at Y{wb-1}")


if __name__ == "__main__":
    main()
