"""diag_lake_xsection.py — cross-section a lake to show BOWL vs flat/staircase.
For each column: water-surface Y (top water) and bed Y (first solid-non-water
below it). Picks the scan row with the most water, plots bed + water profile.

Usage: py tools/diag_lake_xsection.py <region_dir> <tx> <tz> [--out png]
"""
import sys, struct, zlib, gzip, io
import numpy as np, nbtlib
sys.path.insert(0, "tools")
from diag_satellite import read_chunk, unpack
Y_MIN = -64
WATER = {"water", "flowing_water"}

def column_profiles(path):
    """Return (water_surf, bed) (512,512) int arrays; -9999 where none."""
    wsurf = np.full((512, 512), -9999, np.int32)
    bed   = np.full((512, 512), -9999, np.int32)
    for cz in range(32):
        for cx in range(32):
            ch = read_chunk(path, cx, cz)
            if ch is None: continue
            secs = ch.get("sections") or (ch.root.get("sections") if hasattr(ch,"root") else None)
            if not secs: continue
            # stack section name-cubes top-down per column
            order = sorted(secs, key=lambda s:int(s.get("Y",0)), reverse=True)
            top_w = np.full((16,16), -9999, np.int32)
            top_b = np.full((16,16), -9999, np.int32)
            seen_w = np.zeros((16,16), bool)
            for sec in order:
                arr, names = unpack(sec)
                if arr is None: continue
                base = int(sec.get("Y",0))*16
                nm = np.array(names, object); cube = nm[arr]  # [y][z][x]
                for ly in range(15,-1,-1):
                    layer = cube[ly]; yy = base+ly+Y_MIN
                    is_w = np.isin(layer, list(WATER))
                    is_air = np.isin(layer, ["air","cave_air","void_air"])
                    is_solid = ~is_air & ~is_w
                    # first water from top
                    nw = (top_w==-9999) & is_w
                    top_w[nw] = yy
                    # bed = first solid encountered AFTER we've seen water (or top solid if lake dry)
                    seen_w |= is_w
                    nb = (top_b==-9999) & is_solid
                    top_b[nb] = yy
            oz, ox = cz*16, cx*16
            wsurf[oz:oz+16, ox:ox+16] = top_w
            bed[oz:oz+16, ox:ox+16] = top_b
    return wsurf, bed

def main():
    rd, tx, tz = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    out = "diag_verify/lake_xsection.png"
    if "--out" in sys.argv: out = sys.argv[sys.argv.index("--out")+1]
    path = f"{rd}/r.{tx}.{tz}.mca"
    wsurf, bed = column_profiles(path)
    water = wsurf > -9999
    if not water.any():
        print("NO WATER in tile"); return
    rowcounts = water.sum(axis=1)
    r = int(np.argmax(rowcounts))
    print(f"tile ({tx},{tz}) water cells={int(water.sum())}; densest row z-idx={r} ({rowcounts[r]} water cols)")
    wl = wsurf[r]; bd = bed[r]; wcols = np.where(wl>-9999)[0]
    c0, c1 = wcols.min(), wcols.max()
    seg_w = wl[c0:c1+1]; seg_b = bd[c0:c1+1]
    valid = seg_b>-9999
    depth = np.where(valid, seg_w-seg_b, 0)
    wl_const = seg_w[valid]
    print(f"  water span cols {c0}..{c1} ({c1-c0+1} wide)")
    print(f"  water level: min={wl_const.min()} max={wl_const.max()} (flat lake => constant)")
    print(f"  bed Y: min={seg_b[valid].min()} max={seg_b[valid].max()}")
    print(f"  depth: max={depth.max()} mean={depth[valid].mean():.1f}")
    # quantify staircase vs bowl: count distinct bed levels + monotonic dip
    levels = np.unique(seg_b[valid])
    print(f"  distinct bed levels across span: {len(levels)} (few+wide flats => staircase; many+smooth => bowl)")
    # bed slope continuity: how many adjacent steps > 2 blocks
    db = np.abs(np.diff(seg_b[valid]))
    print(f"  adjacent bed jumps >2: {(db>2).sum()}/{len(db)} (a bowl has small smooth steps)")
    try:
        import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
        xs = np.arange(c0,c1+1)
        plt.figure(figsize=(12,4))
        plt.fill_between(xs[valid], seg_b[valid], seg_w[valid], color="#4a90d9", alpha=.6, label="water")
        plt.plot(xs[valid], seg_b[valid], color="#7a5230", lw=2, label="bed")
        plt.plot(xs[valid], seg_w[valid], color="#1f4e9c", lw=1, ls="--", label="water level")
        plt.title(f"Lake cross-section r.{tx}.{tz}.mca row z-idx {r}  (bowl = smooth dip, staircase = flat steps)")
        plt.xlabel("column x"); plt.ylabel("Y"); plt.legend(); plt.grid(alpha=.3)
        plt.tight_layout(); plt.savefig(out, dpi=90); print(f"wrote {out}")
    except Exception as e:
        print("plot skipped:", e)

if __name__ == "__main__":
    main()
