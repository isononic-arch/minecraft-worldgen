"""diag_mca_water.py — read a rendered region (.mca) and visualize water bodies.

Produces, per region:
  - <out>_topdown.png : terrain height (grey) + water surface (blue, darker=deeper)
  - <out>_depth.png   : per-column water depth heatmap (how many water blocks)
  - prints depth histogram + bed/surface stats so we can tell a 1-deep
    nearest-neighbor staircase from a proper carved bowl WITHOUT going in-game.
  - optional cross-section plot through a given world Z row (--xsec Z).

Usage:
  py tools/diag_mca_water.py <r.X.Z.mca> [more.mca ...] [--out DIR] [--xsec Zworld]

Categories: air=0, water/ice=1, solid=2.  Y levels -64..703 (768).
"""
import struct, zlib, gzip, io, sys, os
import numpy as np
import nbtlib

Y_MIN = -64
Y_LEVELS = 768            # -64 .. 703
WATER_NAMES = {"water", "ice", "frosted_ice", "bubble_column", "kelp", "kelp_plant", "seagrass", "tall_seagrass"}
# only true fluids count as "water depth"; plants don't.  keep it tight:
FLUID_NAMES = {"water", "flowing_water"}


def read_chunk(mca_path, lx, lz):
    with open(mca_path, "rb") as f:
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
        raise ValueError(f"comp {comp_type}")
    return nbtlib.File.parse(io.BytesIO(raw))


def unpack_section_cat(section):
    """Return (16,16,16) int8 category array [y][z][x]: 0 air,1 fluid,2 solid."""
    bs = section.get("block_states") if "block_states" in section else section.get("BlockStates")
    if bs is None:
        return None
    palette = bs.get("palette")
    if palette is None:
        return None
    names = [str(e.get("Name")).replace("minecraft:", "").split("[")[0] for e in palette]
    cat_lut = np.zeros(len(names), dtype=np.int8)
    for i, nm in enumerate(names):
        if nm in FLUID_NAMES:
            cat_lut[i] = 1
        elif nm == "air" or nm == "cave_air" or nm == "void_air":
            cat_lut[i] = 0
        else:
            cat_lut[i] = 2
    data = bs.get("data")
    if data is None or len(palette) == 1:
        return np.full((16, 16, 16), cat_lut[0], dtype=np.int8)
    bits = max(4, (len(palette) - 1).bit_length())
    longs = np.array([int(x) & 0xFFFFFFFFFFFFFFFF for x in data], dtype=np.uint64)
    bpl = 64 // bits
    mask = np.uint64((1 << bits) - 1)
    n_longs = len(longs)
    # 1.18+ padded format: block_idx = long_index * bpl + slot. Build a
    # (n_longs, bpl) matrix then ravel ROW-MAJOR so order is li-major/slot-minor.
    vals = np.zeros((n_longs, bpl), dtype=np.int64)
    for slot in range(bpl):
        vals[:, slot] = ((longs >> np.uint64(slot * bits)) & mask).astype(np.int64)
    idxs = vals.reshape(-1)[:4096]
    cat = cat_lut[idxs].reshape(16, 16, 16)   # [y][z][x]
    return cat


def analyze_region(mca_path):
    """Return surf_y, water_top_y, water_bot_y arrays (512x512), -9999 = none."""
    surf = np.full((512, 512), -9999, dtype=np.int32)     # top solid (bed/ground)
    wtop = np.full((512, 512), -9999, dtype=np.int32)     # top water
    wbot = np.full((512, 512), -9999, dtype=np.int32)     # bottom of the top contiguous water body
    yidx = np.arange(Y_LEVELS)[:, None, None]             # for argmax tricks
    for cz in range(32):
        for cx in range(32):
            ch = read_chunk(mca_path, cx, cz)
            if ch is None:
                continue
            secs = ch.get("sections") or (ch.root.get("sections") if hasattr(ch, "root") else None)
            if secs is None:
                continue
            col = np.zeros((Y_LEVELS, 16, 16), dtype=np.int8)
            for sec in secs:
                sy = int(sec.get("Y", 0))
                base = sy * 16 - Y_MIN
                if base < 0 or base + 16 > Y_LEVELS:
                    continue
                cat = unpack_section_cat(sec)
                if cat is not None:
                    col[base:base + 16] = cat
            # top solid
            is_solid = (col == 2)
            has_solid = is_solid.any(axis=0)
            top_solid = (Y_LEVELS - 1) - np.argmax(is_solid[::-1], axis=0)
            top_solid = np.where(has_solid, top_solid + Y_MIN, -9999)
            # top water
            is_water = (col == 1)
            has_water = is_water.any(axis=0)
            top_water = (Y_LEVELS - 1) - np.argmax(is_water[::-1], axis=0)
            top_water_y = np.where(has_water, top_water + Y_MIN, -9999)
            # bottom of contiguous water from top: scan down from top_water while water
            # vectorized-ish: bed under water = highest solid below top water
            # we approximate water_bot as top_solid+1 where solid under water (bowl bed)
            ox, oz = cx * 16, cz * 16
            # top_solid/top_water_y are [z_local][x_local]; store directly.
            surf[oz:oz + 16, ox:ox + 16] = top_solid
            wtop[oz:oz + 16, ox:ox + 16] = top_water_y
            # water depth = top_water - top_solid (solid is the bed beneath)
    return surf, wtop


def render(mca_path, out_dir, xsec_zworld=None):
    from PIL import Image
    name = os.path.splitext(os.path.basename(mca_path))[0]
    surf, wtop = analyze_region(mca_path)
    has_water = wtop > -9999
    depth = np.where(has_water & (surf > -9999), wtop - surf, 0)
    depth = np.clip(depth, 0, None)

    # ---- stats (split sea-level <=63 from inland lakes/rivers >63)
    SEA = 63
    nwat = int(has_water.sum())
    inland = has_water & (wtop > SEA)
    nin = int(inland.sum())
    print(f"\n== {name} ==  water_cols={nwat}  inland(>Y{SEA})={nin}  ({100*nwat/(512*512):.1f}% region)")

    def _depth_report(mask, label):
        m = mask & (depth > 0)
        dvals = depth[m]
        if not dvals.size:
            print(f"   [{label}] none"); return
        hist = np.bincount(np.clip(dvals, 0, 30))
        one = int((dvals == 1).sum())
        wl = wtop[mask]
        print(f"   [{label}] cols={dvals.size} depth min/max/mean={dvals.min()}/{dvals.max()}/{dvals.mean():.1f}"
              f"  1-deep={one}({100*one/dvals.size:.0f}%)  >=4deep={int((dvals>=4).sum())}")
        print(f"   [{label}] surfaceY min/max={wl.min()}/{wl.max()}"
              f"  hist " + " ".join(f"{d}:{c}" for d, c in enumerate(hist) if c))

    if nwat:
        _depth_report(inland, "INLAND")
        _depth_report(has_water & (wtop <= SEA), "sea<=63")

    # ---- topdown PNG: grey terrain + blue water (darker = deeper)
    img = np.zeros((512, 512, 3), dtype=np.uint8)
    sv = surf.astype(np.float32)
    valid = surf > -9999
    if valid.any():
        lo, hi = np.percentile(sv[valid], [2, 98])
        g = np.clip((sv - lo) / max(1, hi - lo), 0, 1)
        grey = (60 + g * 160).astype(np.uint8)
        for c in range(3):
            img[..., c] = np.where(valid, grey, 0)
    # water overlay
    dn = np.clip(depth / 10.0, 0, 1)
    img[..., 0] = np.where(has_water, (30 * (1 - dn)).astype(np.uint8), img[..., 0])
    img[..., 1] = np.where(has_water, (120 + 80 * (1 - dn)).astype(np.uint8), img[..., 1])
    img[..., 2] = np.where(has_water, (180 + 75 * dn).astype(np.uint8), img[..., 2])
    os.makedirs(out_dir, exist_ok=True)
    Image.fromarray(img).save(os.path.join(out_dir, f"{name}_topdown.png"))

    # ---- depth heatmap
    dh = np.zeros((512, 512, 3), dtype=np.uint8)
    dmax = max(1, int(depth.max()))
    dn2 = np.clip(depth / dmax, 0, 1)
    dh[..., 0] = (dn2 * 255).astype(np.uint8)
    dh[..., 2] = ((1 - dn2) * 255 * has_water).astype(np.uint8)
    dh[~has_water] = 0
    Image.fromarray(dh).save(os.path.join(out_dir, f"{name}_depth.png"))
    print(f"   wrote {name}_topdown.png + {name}_depth.png  (depth max={dmax})")

    # ---- cross section through a world-Z row
    if xsec_zworld is not None:
        # region origin in world coords
        parts = name.split(".")
        rx, rz = int(parts[1]), int(parts[2])
        z_local = xsec_zworld - rz * 512
        if 0 <= z_local < 512:
            print(f"   xsec @ worldZ={xsec_zworld} (local {z_local}):")
            row_bed = surf[z_local]
            row_w = wtop[z_local]
            # print compact ascii profile every 8 blocks
            for x0 in range(0, 512, 32):
                seg = []
                for x in range(x0, min(x0 + 32, 512), 4):
                    b = row_bed[x]
                    w = row_w[x]
                    if w > -9999:
                        seg.append(f"{x}:b{b}/w{w}")
                if seg:
                    print("     " + "  ".join(seg))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    out_dir = "diag_water"
    xsec = None
    paths = []
    i = 0
    while i < len(args):
        if args[i] == "--out":
            out_dir = args[i + 1]; i += 2
        elif args[i] == "--xsec":
            xsec = int(args[i + 1]); i += 2
        else:
            paths.append(args[i]); i += 1
    for p in paths:
        if not os.path.exists(p):
            print(f"MISSING: {p}"); continue
        render(p, out_dir, xsec)
