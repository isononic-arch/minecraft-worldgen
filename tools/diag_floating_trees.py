"""diag_floating_trees.py — detect floating trees from the RENDERED MCA: a log
block with AIR or WATER directly below its base (= trunk not grounded). The S94c
water passes (fill-to-banks + re-locks) reshape bank surface_y AFTER schematics
anchor (Step 8), so banks are the risk zone. Flags floating bases near water.

Usage: py tools/diag_floating_trees.py <mca_dir> <tx> <tz>
"""
import sys
import numpy as np
sys.path.insert(0, "tools")
from diag_mca_surface import read_chunk, unpack_section

AIR = {"minecraft:air", "minecraft:void_air", "minecraft:cave_air"}
WATER = {"minecraft:water", "minecraft:bubble_column"}
Y_LO, Y_HI = 50, 330


def main(mdir, tx, tz):
    mca = f"{mdir}/r.{tx}.{tz}.mca"
    float_air = []
    float_water = []
    for cz in range(32):
        for cx in range(32):
            ch = read_chunk(mca, cx, cz)
            if ch is None:
                continue
            secmap = {}
            for sec in (ch.get("sections") or ch.root.get("sections")):
                y = int(sec.get("Y", -999))
                if Y_LO // 16 - 1 <= y <= Y_HI // 16 + 1:
                    g = unpack_section(sec)
                    if g is not None:
                        secmap[y] = g

            def blk(wy, lz, lx):
                g = secmap.get(wy >> 4)
                return g[wy & 15][lz][lx] if g is not None else "minecraft:air"

            for lz in range(16):
                for lx in range(16):
                    # lowest log in the column
                    low_log = None
                    for wy in range(Y_LO, Y_HI + 1):
                        b = blk(wy, lz, lx)
                        if "log" in b or "_wood" in b or "stem" in b:
                            low_log = wy
                            break
                    if low_log is None or low_log <= Y_LO:
                        continue
                    # require a log DIRECTLY ABOVE -> this is a vertical TRUNK base,
                    # not an overhanging branch tip (which over-counts forest edges).
                    above = blk(low_log + 1, lz, lx)
                    if not ("log" in above or "_wood" in above or "stem" in above):
                        continue
                    below = blk(low_log - 1, lz, lx)
                    z = cz * 16 + lz; x = cx * 16 + lx
                    if below in WATER:
                        float_water.append((tx * 512 + x, tz * 512 + z, low_log))
                    elif below in AIR:
                        float_air.append((tx * 512 + x, tz * 512 + z, low_log))
    print(f"=== ({tx},{tz}) FLOATING-TREES: air-below={len(float_air)} "
          f"water-below={len(float_water)} ===")
    for tag, lst in (("air", float_air), ("water", float_water)):
        for (wx, wz, y) in lst[:6]:
            print(f"   {tag}-below  world({wx},{wz}) trunk-base Y{y}")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
