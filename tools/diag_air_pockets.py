"""Find enclosed AIR pockets in a rendered tile (air with a solid block ABOVE it
= a gap in the column) and dump the block stack so we can see what produced them.

Usage: py tools/diag_air_pockets.py <mca_dir> <tx> <tz>
"""
import sys
import numpy as np
sys.path.insert(0, "tools")
from diag_mca_surface import read_chunk, unpack_section

AIRS = {"minecraft:air", "minecraft:void_air", "minecraft:cave_air"}
WATER = {"minecraft:water", "minecraft:bubble_column"}
# a real ground gap = air capped by a TERRAIN block (not tree/plant/water)
GROUND = ("dirt", "grass_block", "podzol", "mud", "coarse", "sand", "gravel",
          "stone", "andesite", "diorite", "granite", "deepslate", "tuff",
          "calcite", "basalt", "blackstone", "sandstone", "terracotta", "clay",
          "snow_block", "moss", "mycelium", "rooted", "cobble", "concrete")
Y_LO, Y_HI = 55, 120


def is_ground(b):
    return any(g in b for g in GROUND)


def main(mdir, tx, tz):
    mca = f"{mdir}/r.{tx}.{tz}.mca"
    pockets = []
    for cz in range(32):
        for cx in range(32):
            ch = read_chunk(mca, cx, cz)
            if ch is None:
                continue
            secs = ch.get("sections") or ch.root.get("sections")
            secmap = {}
            for sec in secs:
                y = int(sec.get("Y", -999))
                if Y_LO // 16 - 1 <= y <= Y_HI // 16 + 1:
                    g = unpack_section(sec)
                    if g is not None:
                        secmap[y] = g
            for lz in range(16):
                for lx in range(16):
                    col = []
                    for wy in range(Y_HI, Y_LO - 1, -1):
                        g = secmap.get(wy >> 4)
                        col.append(g[wy & 15][lz][lx] if g is not None else "minecraft:air")
                    # col is top->down. find a solid above an air that has solid/water below
                    cap = False
                    for i, b in enumerate(col):
                        if is_ground(b) or b in WATER:
                            cap = True            # ground OR water is a real cap
                        elif b not in AIRS:
                            cap = False           # leaves/log/plant -> not a cap
                        elif cap and b in AIRS:
                            # air directly below ground/water -> real pocket
                            below = col[i+1:i+5]
                            if any(is_ground(bb) or bb in WATER for bb in below):
                                z = cz*16+lz; x = cx*16+lx
                                top_y = Y_HI - i
                                pockets.append((z, x, top_y, col[max(0, i-2):i+4]))
                                break
    print(f"({tx},{tz}) enclosed air pockets (solid above air w/ solid/water below): {len(pockets)}")
    for (z, x, ay, stack) in pockets[:20]:
        st = [s.replace("minecraft:", "") for s in stack]
        print(f"  ({z},{x}) world({tx*512+x},{tz*512+z}) air-top~Y{ay}  stack(top->dn)={st}")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
