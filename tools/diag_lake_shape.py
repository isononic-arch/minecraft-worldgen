"""diag_lake_shape.py — lake quality gate from a dry-run surface_y dump.

Derives the waterline from surface_y vs the lake water level (so before/after
are apples-to-apples without needing chunk_writer water blocks), then renders:
  (1) PLAN-VIEW shoreline   — geometric (Balmorhea) vs organic (Nasworthy)
  (2) CROSS-SECTION         — pool wall vs graded slope + bowl
and prints metrics:
  - shore->land step (max height jump at the waterline; big = pool wall)
  - basin fill fraction   (water cells / basin cells; low = dry/striped)
  - outline corner-iness  (fraction of boundary that is axis-aligned runs)

Usage: py tools/diag_lake_shape.py <surface_y.npy> <tx> <tz> <masks_dir> --out <prefix>
"""
import sys, os
import numpy as np, rasterio, json
from rasterio.windows import Window
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt

def water_level_mc(masks_dir, tx, tz, cfg):
    sp = cfg["terrain_spline"]; gin = np.array(sp["gaea_in"], float); gout = np.array(sp["mc_y_out"], float)
    with rasterio.open(os.path.join(masks_dir, "hydro_lake_wl.tif")) as ds:
        wl = ds.read(1, window=Window(tx*512, tz*512, 512, 512)).astype(np.float32)
    with rasterio.open(os.path.join(masks_dir, "hydro_lake.tif")) as ds:
        lid = ds.read(1, window=Window(tx*512, tz*512, 512, 512))
    basin = lid > 0
    wy = np.interp(wl*65535.0, gin, gout)   # per-cell water level (MC Y)
    return basin, wy, lid

def main():
    syf = sys.argv[1]; tx = int(sys.argv[2]); tz = int(sys.argv[3]); md = sys.argv[4]
    out = "diag_lake/lake";
    if "--out" in sys.argv: out = sys.argv[sys.argv.index("--out")+1]
    cfg = json.load(open("config/thresholds.json"))
    sy = np.load(syf).astype(np.float32)
    basin, wy, lid = water_level_mc(md, tx, tz, cfg)
    if not basin.any():
        print(f"({tx},{tz}) NO BASIN in tile"); return
    wl_flat = float(np.median(wy[basin]))            # flat lake level
    # Prefer the TRUE rendered lake mask (river_meta==CHAN_LAKE=3) if dumped next
    # to the surface_y — that's exactly what renders as water. Fall back to the
    # basin&(sy<level) approximation otherwise.
    rmeta_path = syf.replace("sy_post", "rmeta")
    if os.path.exists(rmeta_path):
        water = np.load(rmeta_path) == 3
        print(f"  [water = river_meta==CHAN_LAKE, true rendered mask]")
    else:
        water = basin & (sy < wl_flat)
        print(f"  [water = basin&(sy<level) approximation]")
    print(f"=== lake ({tx},{tz}) ===")
    print(f"  basin cells={int(basin.sum())}  water cells={int(water.sum())}  "
          f"fill={100*water.sum()/max(1,basin.sum()):.1f}%  level=Y{wl_flat:.0f}")
    if not water.any():
        print("  (no water under level — fully dry/striped)");
    # shore->land step: waterline cells vs their highest 4-neighbour land cell
    edge = water & (~np.pad(water,1)[2:,1:-1] | ~np.pad(water,1)[:-2,1:-1]
                    | ~np.pad(water,1)[1:-1,2:] | ~np.pad(water,1)[1:-1,:-2])
    steps = []
    ys, xs = np.where(edge)
    for y, x in zip(ys, xs):
        nb = []
        for dy,dx in ((1,0),(-1,0),(0,1),(0,-1)):
            yy,xx=y+dy,x+dx
            if 0<=yy<512 and 0<=xx<512 and not water[yy,xx]:
                nb.append(sy[yy,xx]-wl_flat)        # land height above water
        if nb: steps.append(max(nb))
    steps = np.array(steps) if steps else np.array([0.0])
    print(f"  shore->land step: mean={steps.mean():+.1f} p90={np.percentile(steps,90):+.1f} "
          f"max={steps.max():+.1f}  (>~4 = pool wall)")
    # outline corner-iness: fraction of boundary pixels whose row OR col run >=4 (axis-aligned)
    # plan-view render
    img = np.full((512,512,3), (210,198,150), np.uint8)   # land tan
    img[water] = (60,110,200)                              # water blue
    img[edge]  = (20,40,90)                                # shoreline dark
    plt.imsave(out+"_planview.png", img)
    # cross-section: densest water row, full width
    r = int(np.argmax(water.sum(1)))
    bed = sy[r]; lvl = np.full(512, wl_flat)
    plt.figure(figsize=(12,3.5))
    xs2=np.arange(512)
    wmask = water[r]
    plt.fill_between(xs2, bed, np.where(wmask, lvl, bed), color="#4a90d9", alpha=.6)
    plt.plot(xs2, bed, color="#7a5230", lw=1.5, label="surface (bed/land)")
    plt.axhline(wl_flat, color="#1f4e9c", ls="--", lw=1, label=f"water Y{wl_flat:.0f}")
    plt.title(f"lake ({tx},{tz}) row {r} — wall vs graded shore")
    plt.legend(); plt.grid(alpha=.3); plt.tight_layout(); plt.savefig(out+"_xsection.png", dpi=90)
    print(f"  wrote {out}_planview.png + {out}_xsection.png")

if __name__ == "__main__":
    main()
