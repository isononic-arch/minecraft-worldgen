"""diag_river_xsection.py — channel cross-section profiler from the RENDERED MCA.
Shows terrain vs water vs banks across a river/lake so we can SEE "water touching
air" (water surface above neighbour terrain) and "steep banks nearly all dry"
(bank slope above the water line). Saves a PNG profile + prints a text summary.

Usage: py tools/diag_river_xsection.py <mca_dir> <tx> <tz> [out.png] [n_rows]
Auto-picks the n_rows rows with the most river water (excludes lakes via mask).
"""
import sys
import numpy as np
import rasterio
sys.path.insert(0, "tools")
from diag_mca_surface import read_chunk, unpack_section

WATER = {"minecraft:water", "minecraft:bubble_column"}
AIR = {"minecraft:air", "minecraft:void_air", "minecraft:cave_air"}
PLANT = ("leaves", "log", "grass", "fern", "bush", "flower", "seagrass", "kelp",
         "vine", "mushroom", "sapling", "bamboo", "cactus", "sugar", "lily",
         "dead_bush", "snow")  # not-terrain caps (snow=surface dusting)
Y_LO, Y_HI = 40, 330


def surfaces(mca):
    """Return water_top, terrain_top (highest solid that's not water/plant) per cell."""
    wt = np.full((512, 512), -999, np.int32)
    tt = np.full((512, 512), -999, np.int32)
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
            for lz in range(16):
                for lx in range(16):
                    z = cz * 16 + lz; x = cx * 16 + lx
                    w = -999; t = -999
                    for wy in range(Y_HI, Y_LO - 1, -1):
                        g = secmap.get(wy >> 4)
                        if g is None:
                            continue
                        b = g[wy & 15][lz][lx]
                        if b in AIR:
                            continue
                        if w == -999 and b in WATER:
                            w = wy
                        if t == -999 and b not in WATER and not any(p in b for p in PLANT):
                            t = wy
                        if t != -999 and (w != -999 or wy < (w if w > 0 else Y_HI)):
                            if t != -999 and w != -999:
                                break
                            if t != -999 and w == -999:
                                break
                    wt[z, x] = w; tt[z, x] = t
    return wt, tt


def main(mdir, tx, tz, out=None, n_rows=3):
    mca = f"{mdir}/r.{tx}.{tz}.mca"
    wt, tt = surfaces(mca)
    z0, x0 = tz * 512, tx * 512
    with rasterio.open("masks/hydro_lake.tif") as s:
        lake = s.read(1, window=((z0, z0 + 512), (x0, x0 + 512))) > 0
    with rasterio.open("masks/hydro_centerline.tif") as s:
        cl = s.read(1, window=((z0, z0 + 512), (x0, x0 + 512))) > 0
    riverW = (wt > -999) & cl & ~lake
    rows = np.argsort(-riverW.sum(axis=1))[:n_rows]

    out = out or f"xsection_{tx}_{tz}.png"
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, axes = plt.subplots(len(rows), 1, figsize=(14, 3.2 * len(rows)))
        if len(rows) == 1:
            axes = [axes]
    except Exception:
        axes = None

    print(f"=== ({tx},{tz}) cross-sections (top {n_rows} river rows) ===")
    for i, z in enumerate(rows):
        terr = tt[z].astype(float); wat = wt[z].astype(float)
        haswat = wat > -900
        # crop to the wet span + margin
        xs = np.where(haswat)[0]
        if xs.size == 0:
            continue
        a, b = max(0, xs.min() - 25), min(511, xs.max() + 25)
        xr = np.arange(a, b + 1)
        terr_c = terr[a:b + 1]; wat_c = wat[a:b + 1]; hw = haswat[a:b + 1]
        # metrics: water level (mode of water-top), water width, channel(terr<level) width,
        # dry-bank cells (terr above the water level but within the channel span),
        # perch cells (water-top > a 4-neighbour terrain by >1)
        lvl = int(np.median(wat_c[hw])) if hw.any() else -999
        wwidth = int(hw.sum())
        below_lvl = int(((terr_c < lvl) & ~hw).sum())   # cells below water level but DRY = should be wet
        # exposed water = water-top above either horizontal neighbour terrain
        exposed = 0
        wtr = wt[z]
        for x in xr[hw[xr - a] if False else (wat[xr] > -900)]:
            for dx in (-1, 1):
                nx = x + dx
                if 0 <= nx < 512 and tt[z, nx] > -999 and wt[z, nx] < -900 and wtr[x] > tt[z, nx] + 1:
                    exposed += 1; break
        print(f"  row z={z0+z}: water_lvl~Y{lvl}  water_width={wwidth}  "
              f"dry-but-below-level={below_lvl}  exposed-water-faces={exposed}")
        if axes is not None:
            ax = axes[i]
            ax.fill_between(xr + x0, Y_LO, terr_c, color="#7a5a3a", step="mid", label="terrain")
            wsurf = np.where(hw, wat_c, np.nan)
            wbed = np.where(hw, np.minimum(terr_c, wat_c), np.nan)
            ax.fill_between(xr + x0, wbed, wsurf, color="#2a6cc0", step="mid", alpha=0.8, label="water")
            if lvl > -900:
                ax.axhline(lvl, color="#1a4a8a", lw=0.6, ls=":")
            ax.set_title(f"({tx},{tz}) z={z0+z}  water~Y{lvl} w={wwidth} dry-below-lvl={below_lvl} exposed={exposed}",
                         fontsize=9)
            ax.set_ylim(lvl - 12 if lvl > -900 else Y_LO, (lvl if lvl > -900 else Y_HI) + 18)
            ax.legend(fontsize=7, loc="upper right")
    if axes is not None:
        plt.tight_layout(); plt.savefig(out, dpi=90); print(f"  saved {out}")


if __name__ == "__main__":
    a = sys.argv
    main(a[1], int(a[2]), int(a[3]), a[4] if len(a) > 4 else None,
         int(a[5]) if len(a) > 5 else 3)
