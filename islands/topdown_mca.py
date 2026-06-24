"""topdown_mca.py — render a color top-down of an island's WRITTEN .mca region
files (topmost solid block per column), to verify the offset-render landed right.

Usage: py islands/topdown_mca.py --name fogo [--maxpx 1400]
"""
import sys, struct, zlib, gzip, io, json, math, argparse
from pathlib import Path
import numpy as np
import nbtlib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.preview_renderer import BLOCK_COLORS

ISL = ROOT / "islands"
SECTION_H = 16


def _safe(n):
    import re
    return re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")


def read_chunk(f, lx, lz):
    idx = (lz % 32) * 32 + (lx % 32)
    f.seek(idx * 4); loc = f.read(4)
    off = (loc[0] << 16) | (loc[1] << 8) | loc[2]; secs = loc[3]
    if off == 0 or secs == 0:
        return None
    f.seek(off * 4096); length = struct.unpack(">I", f.read(4))[0]
    ct = f.read(1)[0]; data = f.read(length - 1)
    raw = gzip.decompress(data) if ct == 1 else zlib.decompress(data) if ct == 2 else data
    return nbtlib.File.parse(io.BytesIO(raw))


def section_blocks(sec):
    bs = sec.get("block_states")
    if bs is None:
        return None
    pal = bs.get("palette")
    if pal is None:
        return None
    names = [str(p.get("Name", "minecraft:air")) for p in pal]
    if len(names) == 1:
        return np.full((16, 16, 16), names[0], dtype=object)
    data = bs.get("data")
    if data is None:
        return np.full((16, 16, 16), names[0], dtype=object)
    bits = max(4, (len(names) - 1).bit_length())
    per = 64 // bits
    mask = np.uint64((1 << bits) - 1)
    longs = np.asarray(data, dtype=np.uint64)            # 1.18+ padded long array
    shifts = (np.arange(per, dtype=np.uint64) * np.uint64(bits))
    idx = ((longs[:, None] >> shifts[None, :]) & mask).reshape(-1)[:4096].astype(np.int64)
    arr = np.array(names, dtype=object)[np.clip(idx, 0, len(names) - 1)]
    return arr.reshape(16, 16, 16)   # (y,z,x)


def top_block_for_chunk(chunk):
    """Return (16,16) topmost non-air block names indexed [z,x]."""
    lvl = chunk
    secs = lvl.get("sections") or lvl.get("Sections") or []
    by_y = {}
    for s in secs:
        y = int(s.get("Y", 0)); b = section_blocks(s)
        if b is not None:
            by_y[y] = b
    top = np.full((16, 16), "minecraft:air", dtype=object)
    found = np.zeros((16, 16), bool)
    for y in sorted(by_y.keys(), reverse=True):
        blk = by_y[y]
        for yy in range(15, -1, -1):
            layer = blk[yy]            # (z,x)
            fill = (~found) & (layer != "minecraft:air") & (layer != "minecraft:cave_air") \
                   & (layer != "minecraft:void_air")
            top[fill] = layer[fill]; found |= fill
            if found.all():
                return top
    return top


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--maxpx", type=int, default=1400)
    a = ap.parse_args()
    layout = json.loads((ISL / "layout.json").read_text())
    entry = next(i for i in layout["islands"] if a.name in _safe(i["name"]) or a.name in i["dem_path"])
    name = _safe(entry["name"])
    odir = ISL / "out" / name
    man = json.loads((ISL / "masks_islands" / name / "manifest.json").read_text())
    ox, oz = man["world_offset_px"]; wh, ww = man["world_hw"]
    # world bbox
    x0, z0 = ox, oz; x1, z1 = ox + ww, oz + wh
    step = max(1, max(ww, wh) // a.maxpx)
    img = np.zeros(((z1 - z0)//step + 1, (x1 - x0)//step + 1, 3), np.uint8)
    img[:] = (18, 28, 46)
    DEF = (120, 120, 120)
    nfiles = 0
    for mca in sorted(odir.glob("r.*.mca")):
        rx, rz = map(int, mca.stem.split(".")[1:3])
        with open(mca, "rb") as f:
            for lz in range(32):
                for lx in range(32):
                    try:
                        ch = read_chunk(f, lx, lz)
                    except Exception:
                        ch = None
                    if ch is None:
                        continue
                    top = top_block_for_chunk(ch)
                    cwx = (rx * 32 + lx) * 16; cwz = (rz * 32 + lz) * 16
                    for zz in range(0, 16, step):
                        for xx in range(0, 16, step):
                            wx = cwx + xx; wz = cwz + zz
                            if not (x0 <= wx < x1 and z0 <= wz < z1):
                                continue
                            nm = str(top[zz, xx]).replace("minecraft:", "")
                            if nm in ("air", "cave_air", "void_air"):
                                continue
                            col = BLOCK_COLORS.get(nm, DEF)
                            img[(wz - z0)//step, (wx - x0)//step] = col
        nfiles += 1
    from PIL import Image
    # auto-crop to rendered content (trim empty ocean border) so islands fill the frame
    bg = np.array([18, 28, 46], np.uint8)
    content = np.any(img != bg, axis=2)
    if content.any():
        ys, xs = np.where(content)
        m = 12
        y0c, y1c = max(0, ys.min()-m), min(img.shape[0], ys.max()+m)
        x0c, x1c = max(0, xs.min()-m), min(img.shape[1], xs.max()+m)
        img = img[y0c:y1c, x0c:x1c]
    out = odir / "topdown.png"
    Image.fromarray(img).save(out)
    print(f"saved {out}  ({nfiles} region files, {img.shape[1]}x{img.shape[0]})", flush=True)


if __name__ == "__main__":
    main()
