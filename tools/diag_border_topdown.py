"""diag_border_topdown.py — S93: top-down rendered-block map of a tile-border
strip, the first-class VISUAL gate for seam work (S92 postmortem: numeric
gates passed while walks failed; eyeball the blocks before any box render).

Reads two adjacent .mca region files and paints the top block of every
column in a window spanning their shared border. Red line = the border.

Usage:
  py tools/diag_border_topdown.py <A.mca> <B.mca> <txA> <tzA> <txB> <tzB> \
      [--span 64] [--out diag_verify/border.png]
Only vertical seams (B east of A) are supported.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from diag_mca_surface import read_chunk, unpack_section

COLORS = {
    "grass_block": (90, 143, 60), "short_grass": (110, 160, 70),
    "tall_grass": (110, 160, 70), "fern": (95, 150, 75),
    "large_fern": (95, 150, 75), "moss_block": (80, 130, 70),
    "moss_carpet": (80, 130, 70), "podzol": (122, 82, 48),
    "dirt": (134, 96, 67), "coarse_dirt": (119, 85, 59),
    "rooted_dirt": (119, 85, 59), "mud": (60, 57, 60),
    "packed_mud": (142, 106, 80), "clay": (160, 166, 179),
    "sand": (219, 207, 163), "gravel": (136, 126, 126),
    "stone": (125, 125, 125), "andesite": (136, 136, 137),
    "diorite": (188, 188, 188), "granite": (149, 103, 86),
    "calcite": (224, 220, 210), "tuff": (108, 110, 100),
    "water": (60, 100, 190), "snow": (240, 245, 250),
    "snow_block": (235, 240, 248), "leaf_litter": (140, 100, 60),
    "bush": (70, 110, 50), "azalea": (90, 130, 60),
}


def color_of(name):
    if name is None:
        return (20, 20, 20)
    bare = name.replace("minecraft:", "").split("[")[0]
    if bare in COLORS:
        return COLORS[bare]
    if "leaves" in bare:
        return (40, 90, 35)       # canopy — dark green
    if "log" in bare or "wood" in bare:
        return (84, 60, 36)
    if "water" in bare:
        return (60, 100, 190)
    if "snow" in bare or "ice" in bare:
        return (235, 240, 248)
    return (170, 60, 170)         # unknown — magenta (make it obvious)


def top_block_map(path, rx, rz, wx0, wx1, wz0, wz1):
    out = {}
    cache = {}
    for wx in range(wx0, wx1):
        lx = wx - rx * 512
        if not (0 <= lx < 512):
            continue
        for wz in range(wz0, wz1):
            lz = wz - rz * 512
            if not (0 <= lz < 512):
                continue
            ck = (lx // 16, lz // 16)
            if ck not in cache:
                sa = {}
                try:
                    ch = read_chunk(path, ck[0], ck[1])
                    secs = ch.get("sections") or ch.root.get("sections")
                    for sec in secs or []:
                        arr = unpack_section(sec)
                        if arr is not None:
                            sa[int(sec.get("Y", 0))] = arr
                except Exception:
                    pass
                cache[ck] = sa
            top = None
            for sy in sorted(cache[ck].keys(), reverse=True):
                arr = cache[ck][sy]
                for ly in range(15, -1, -1):
                    b = arr[ly][lz % 16][lx % 16]
                    if b and "air" not in b:
                        top = b
                        break
                if top:
                    break
            out[(wx, wz)] = top
    return out


def main():
    pA, pB = sys.argv[1], sys.argv[2]
    txA, tzA, txB, tzB = map(int, sys.argv[3:7])
    span = int(sys.argv[sys.argv.index("--span") + 1]) if "--span" in sys.argv else 64
    out = (sys.argv[sys.argv.index("--out") + 1]
           if "--out" in sys.argv else "diag_verify/border_topdown.png")
    seam = txB * 512
    wz0, wz1 = tzA * 512, tzA * 512 + 512
    wx0, wx1 = seam - span, seam + span
    A = top_block_map(pA, txA, tzA, wx0, seam, wz0, wz1)
    B = top_block_map(pB, txB, tzB, seam, wx1, wz0, wz1)
    img = np.zeros((wz1 - wz0, wx1 - wx0, 3), dtype=np.uint8)
    for (wx, wz), b in {**A, **B}.items():
        img[wz - wz0, wx - wx0] = color_of(b)
    img[:, span - 1] = np.maximum(img[:, span - 1], (90, 0, 0))  # faint seam tick
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    plt.figure(figsize=(6, 18))
    plt.imshow(img)
    plt.axvline(span - 0.5, color="red", lw=0.6, alpha=0.6)
    plt.title(f"top-down border strip x={wx0}..{wx1}  (seam x={seam}, red)")
    plt.tight_layout()
    plt.savefig(out, dpi=130)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
