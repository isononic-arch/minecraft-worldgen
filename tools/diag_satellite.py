"""diag_satellite.py — top-down hillshaded "satellite" view of rendered tiles.

Reads top-solid surface block + Y per column from one or more region .mca files,
colors by surface block, and applies hillshade (relief shading from the surface_y
gradient) so terrain reads naturally AND tile-boundary seams show as a sharp
relief discontinuity line. Stitches multiple regions by their r.X.Z coords.

Usage: py tools/diag_satellite.py r.73.66.mca r.74.66.mca --out diag_water/sat.png
"""
import struct, zlib, gzip, io, sys, os
import numpy as np
import nbtlib

Y_MIN, Y_LEVELS = -64, 768

# surface block -> base RGB
COL = {
    "grass_block": (95, 150, 70), "dirt": (122, 90, 60), "coarse_dirt": (120, 88, 58),
    "podzol": (90, 64, 40), "sand": (212, 200, 150), "red_sand": (190, 110, 60),
    "stone": (130, 130, 132), "andesite": (140, 140, 142), "granite": (160, 120, 110),
    "diorite": (190, 190, 192), "gravel": (140, 132, 122), "cobblestone": (120, 120, 122),
    "snow_block": (235, 240, 248), "snow": (235, 240, 248), "powder_snow": (230, 236, 245),
    "calcite": (224, 222, 214), "tuff": (110, 116, 92), "cobbled_deepslate": (74, 74, 82),
    "smooth_basalt": (60, 58, 64), "deepslate": (70, 70, 78), "packed_ice": (180, 210, 235),
    "water": (50, 110, 200), "moss_block": (78, 110, 55), "terracotta": (150, 90, 60),
}
WATERY = {"water", "flowing_water", "ice"}


def read_chunk(p, lx, lz):
    with open(p, "rb") as f:
        idx = (lz % 32) * 32 + (lx % 32); f.seek(idx * 4); loc = f.read(4)
        off = (loc[0] << 16) | (loc[1] << 8) | loc[2]
        if off == 0 or loc[3] == 0:
            return None
        f.seek(off * 4096); ln = struct.unpack(">I", f.read(4))[0]; ct = f.read(1)[0]; data = f.read(ln - 1)
    raw = gzip.decompress(data) if ct == 1 else zlib.decompress(data) if ct == 2 else data
    return nbtlib.File.parse(io.BytesIO(raw))


def unpack(sec):
    bs = sec.get("block_states") or sec.get("BlockStates")
    if bs is None:
        return None, None
    pal = bs.get("palette")
    if pal is None:
        return None, None
    names = [str(e.get("Name")).replace("minecraft:", "").split("[")[0] for e in pal]
    data = bs.get("data")
    if data is None or len(pal) == 1:
        return np.zeros((16, 16, 16), np.int32), names
    bits = max(4, (len(pal) - 1).bit_length())
    longs = np.array([int(x) & 0xFFFFFFFFFFFFFFFF for x in data], np.uint64)
    bpl = 64 // bits; mask = np.uint64((1 << bits) - 1); n_longs = len(longs)
    vals = np.zeros((n_longs, bpl), np.int64)
    for slot in range(bpl):
        vals[:, slot] = ((longs >> np.uint64(slot * bits)) & mask).astype(np.int64)
    return vals.reshape(-1)[:4096].reshape(16, 16, 16), names


def region_surface(p):
    sy = np.full((512, 512), -9999, np.int32)
    blk = np.empty((512, 512), object)
    for cz in range(32):
        for cx in range(32):
            ch = read_chunk(p, cx, cz)
            if ch is None:
                continue
            secs = ch.get("sections") or (ch.root.get("sections") if hasattr(ch, "root") else None)
            if not secs:
                continue
            # build name-cube top-down
            top_name = np.full((16, 16), None, object)
            top_y = np.full((16, 16), -9999, np.int32)
            for sec in sorted(secs, key=lambda s: int(s.get("Y", 0)), reverse=True):
                arr, names = unpack(sec)
                if arr is None:
                    continue
                syb = int(sec.get("Y", 0)) * 16
                nm = np.array(names, object)
                cube = nm[arr]  # (16,16,16) [y][z][x] names
                solid = ~np.isin(cube, ["air", "cave_air", "void_air"])
                for ly in range(15, -1, -1):
                    need = (top_y == -9999) & solid[ly]
                    if need.any():
                        top_name[need] = cube[ly][need]
                        top_y[need] = syb + ly + Y_MIN
                if (top_y != -9999).all():
                    break
            oz, ox = cz * 16, cx * 16
            sy[oz:oz + 16, ox:ox + 16] = top_y
            blk[oz:oz + 16, ox:ox + 16] = top_name
    return sy, blk


def render(paths, out):
    from PIL import Image
    # parse coords, find bounds
    tiles = {}
    for p in paths:
        nm = os.path.basename(p).split(".")
        rx, rz = int(nm[1]), int(nm[2])
        tiles[(rx, rz)] = region_surface(p)
    rxs = [k[0] for k in tiles]; rzs = [k[1] for k in tiles]
    x0, z0 = min(rxs), min(rzs)
    W = (max(rxs) - x0 + 1) * 512; H = (max(rzs) - z0 + 1) * 512
    SY = np.full((H, W), -9999, np.int32); BLK = np.empty((H, W), object)
    for (rx, rz), (sy, blk) in tiles.items():
        oz = (rz - z0) * 512; ox = (rx - x0) * 512
        SY[oz:oz + 512, ox:ox + 512] = sy; BLK[oz:oz + 512, ox:ox + 512] = blk
    # base color
    img = np.full((H, W, 3), (90, 90, 95), np.uint8)
    for nm, c in COL.items():
        m = (BLK == nm)
        if m.any():
            for i in range(3):
                img[..., i][m] = c[i]
    # hillshade from surface_y gradient (illum from NW)
    syf = np.where(SY > -9999, SY, np.nan).astype(np.float32)
    syf = np.nan_to_num(syf, nan=np.nanmedian(syf))
    gz, gx = np.gradient(syf)
    shade = np.clip(0.5 + 0.6 * (-gx - gz) / 3.0, 0.25, 1.6)  # NW light
    img = np.clip(img.astype(np.float32) * shade[..., None], 0, 255).astype(np.uint8)
    # tile-seam marker lines OFF by default (they mask real seams). Enable with
    # env SAT_SEAM_LINES=1 if you want them back.
    if os.environ.get("SAT_SEAM_LINES"):
        for sx in range(512 - (x0 * 512) % 512, W, 512):
            if 0 <= sx < W:
                img[:, sx] = (255, 0, 0)
    Image.fromarray(img).save(out)
    print(f"wrote {out}  ({W}x{H}, {len(tiles)} tiles)")


if __name__ == "__main__":
    args = sys.argv[1:]; out = "diag_water/satellite.png"; paths = []
    i = 0
    while i < len(args):
        if args[i] == "--out":
            out = args[i + 1]; i += 2
        else:
            paths.append(args[i]); i += 1
    render(paths, out)
