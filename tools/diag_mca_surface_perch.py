"""Ground-truth perch detector from the RENDERED MCA: build per-column top-water
and top-solid (ground) Y, then flag water cells whose water surface sits ABOVE a
4-neighbour's entire column top (= water spilling over surrounding land). This is
exactly what the user sees in-world.

Usage: py tools/diag_mca_surface_perch.py <mca_dir> <tx> <tz> [tx tz ...]
"""
import sys
import numpy as np
sys.path.insert(0, "tools")
from diag_mca_surface import read_chunk, unpack_section

WATER = {"minecraft:water", "minecraft:bubble_column"}
AIRS = {"minecraft:air", "minecraft:void_air", "minecraft:cave_air"}
NONGROUND = {"minecraft:water", "minecraft:bubble_column", "minecraft:seagrass",
             "minecraft:tall_seagrass", "minecraft:kelp", "minecraft:kelp_plant"}
Y_LO, Y_HI = 50, 320   # raised ceiling so high-altitude rivers (bed Y150-280) are seen


def tile_surface(mca):
    """Return water_top[512,512], col_top[512,512] (-999 where none)."""
    wt = np.full((512, 512), -999, np.int32)
    ct = np.full((512, 512), -999, np.int32)
    for cz in range(32):
        for cx in range(32):
            ch = read_chunk(mca, cx, cz)
            if ch is None:
                continue
            secs = ch.get("sections") or ch.root.get("sections")
            # decode sections covering Y_LO..Y_HI, high to low
            secmap = {}
            for sec in secs:
                y = int(sec.get("Y", -999))
                if Y_LO // 16 - 1 <= y <= Y_HI // 16 + 1:
                    g = unpack_section(sec)
                    if g is not None:
                        secmap[y] = g
            for lz in range(16):
                for lx in range(16):
                    z = cz * 16 + lz; x = cx * 16 + lx
                    w = -999; c = -999
                    for wy in range(Y_HI, Y_LO - 1, -1):
                        g = secmap.get(wy >> 4)
                        if g is None:
                            continue
                        b = g[wy & 15][lz][lx]
                        if b in AIRS:
                            continue
                        if c == -999:
                            c = wy
                        if b in WATER and w == -999:
                            w = wy
                        if c != -999 and (w != -999 or b not in NONGROUND):
                            break
                    wt[z, x] = w; ct[z, x] = c
    return wt, ct


def main(mdir, pairs):
    for tx, tz in pairs:
        mca = f"{mdir}/r.{tx}.{tz}.mca"
        wt, ct = tile_surface(mca)
        water = wt > -999
        dry = (~water) & (ct > -999)          # dry land (solid top, no water)
        INF = 1 << 20
        # min DRY-land neighbour ground-top (the user's complaint: water above DRY land)
        dryv = np.where(dry, ct, INF)
        min_dry = np.full(ct.shape, INF, np.int32)
        # min WATER neighbour surface (water-vs-water step, a different phenomenon)
        watv = np.where(water, wt, -INF)
        max_watnb = np.full(ct.shape, -INF, np.int32)
        for dz, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            min_dry = np.minimum(min_dry, np.roll(np.roll(dryv, dz, 0), dx, 1))
            max_watnb = np.maximum(max_watnb, np.roll(np.roll(watv, dz, 0), dx, 1))
        # DRY-LAND perch: water surface > an adjacent DRY land top (true overspill)
        perch_dry = water & (min_dry < INF) & (wt > min_dry + 1)
        # water-water step: this water > an adjacent WATER surface by >1 (level mismatch)
        step_ww = water & (max_watnb > -INF) & (wt > max_watnb + 1)
        n_w = int(water.sum())
        print(f"=== ({tx},{tz}) water={n_w}  DRY-LAND-PERCH={int(perch_dry.sum())} "
              f"({100.0*perch_dry.sum()/max(1,n_w):.2f}%)  water-water-step={int(step_ww.sum())} ===")
        if perch_dry.any():
            zz, xx = np.where(perch_dry); over = (wt - min_dry)[zz, xx]
            o = np.argsort(-over)[:10]
            print("  worst DRY-LAND perch (water above dry land):")
            for i in o:
                z, x = zz[i], xx[i]
                edge = "EDGE" if (x in (0, 511) or z in (0, 511)) else ""
                print(f"   world({tx*512+x},{tz*512+z}) water={wt[z,x]} dry_land={min_dry[z,x]} "
                      f"-> +{over[i]} {edge}")


if __name__ == "__main__":
    args = sys.argv[2:]
    pairs = [(int(args[i]), int(args[i+1])) for i in range(0, len(args), 2)]
    main(sys.argv[1], pairs)
