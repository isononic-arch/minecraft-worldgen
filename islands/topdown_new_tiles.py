"""topdown_new_tiles.py — color top-down of only the FRESHLY re-rendered island
tiles (mtime newer than a cutoff), with accurate colors for the lithology/veg
blocks that core.preview_renderer.BLOCK_COLORS lacks (so rock reads brown, not
grey-fallback). Crops to content.
Usage: py islands/topdown_new_tiles.py --name new_vincentia --since-min 180
"""
import sys, os, json, time, argparse, glob
from pathlib import Path
import numpy as np
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from islands.topdown_mca import read_chunk, top_block_for_chunk
from core.preview_renderer import BLOCK_COLORS

ISL = ROOT / "islands"

# accurate-ish in-game colors for blocks missing from BLOCK_COLORS (esp. the
# mossy_temperate / lithology rock palette + ground cover)
OVR = {
    "soul_soil": (74, 56, 45), "brown_concrete_powder": (110, 66, 33),
    "raw_iron_block": (196, 170, 145), "mud": (62, 56, 52), "packed_mud": (146, 104, 72),
    "rooted_dirt": (122, 90, 66), "dirt": (122, 90, 66), "coarse_dirt": (112, 82, 56),
    "podzol": (92, 62, 34), "grass_block": (92, 134, 60), "moss_block": (84, 112, 46),
    "pale_moss_block": (150, 158, 120), "pale_moss_carpet": (150, 158, 120),
    "moss_carpet": (92, 122, 52), "leaf_litter": (134, 104, 54),
    "short_grass": (104, 150, 70), "tall_grass": (104, 150, 70), "fern": (96, 140, 66),
    "large_fern": (96, 140, 66), "bush": (84, 112, 50), "dead_bush": (140, 110, 60),
    "gravel": (132, 126, 120), "sand": (214, 198, 152), "clay": (162, 162, 172),
    "stone": (122, 122, 124), "andesite": (130, 130, 130), "water": (40, 72, 130),
    "snow_block": (236, 242, 248), "powder_snow": (236, 242, 248),
    "cobblestone": (118, 118, 120), "tuff": (110, 112, 104), "deepslate": (78, 78, 84),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="new_vincentia")
    ap.add_argument("--since-min", type=float, default=180.0)
    ap.add_argument("--maxpx", type=int, default=1500)
    a = ap.parse_args()
    layout = json.loads((ISL / "layout.json").read_text())
    import re
    def _safe(n): return re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")
    entry = next(i for i in layout["islands"] if a.name in _safe(i["name"]) or a.name in i["dem_path"])
    name = _safe(entry["name"])
    odir = ISL / "out" / name
    cutoff = time.time() - a.since_min * 60
    files = [(int(p.split('.')[-3]), int(p.split('.')[-2]), p)
             for p in glob.glob(str(odir / "r.*.mca")) if os.path.getmtime(p) >= cutoff]
    if not files:
        print("no fresh tiles"); return
    rxs = [f[0] for f in files]; rzs = [f[1] for f in files]
    x0, z0 = min(rxs) * 512, min(rzs) * 512
    x1, z1 = (max(rxs) + 1) * 512, (max(rzs) + 1) * 512
    step = max(1, max(x1 - x0, z1 - z0) // a.maxpx)
    img = np.zeros(((z1 - z0) // step + 1, (x1 - x0) // step + 1, 3), np.uint8)
    img[:] = (18, 28, 46)
    DEF = (120, 120, 120)
    print(f"{len(files)} fresh tiles, region X{min(rxs)}-{max(rxs)} Z{min(rzs)}-{max(rzs)}")
    for rx, rz, mca in files:
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
                            n = str(top[zz, xx]).replace("minecraft:", "")
                            if n in ("air", "cave_air", "void_air"):
                                continue
                            col = OVR.get(n) or BLOCK_COLORS.get(n, DEF)
                            img[(cwz + zz - z0) // step, (cwx + xx - x0) // step] = col
    from PIL import Image
    bg = np.array([18, 28, 46], np.uint8)
    content = np.any(img != bg, axis=2)
    if content.any():
        ys, xs = np.where(content); m = 8
        img = img[max(0, ys.min()-m):ys.max()+m, max(0, xs.min()-m):xs.max()+m]
    out = odir / "topdown_new.png"
    Image.fromarray(img).save(out)
    print(f"saved {out}  ({img.shape[1]}x{img.shape[0]})")


if __name__ == "__main__":
    main()
