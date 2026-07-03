"""_topdown_pairs_s101c.py — stitch each S101c realism test-tile 2x1 pair into a
labeled color topdown (topmost solid block per column), reusing topdown_mca's
chunk readers. One PNG per fix -> islands/_val/topdown_<fix>.png.
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "islands"))
from core.preview_renderer import BLOCK_COLORS                       # noqa: E402
from topdown_mca import read_chunk, top_block_for_chunk             # noqa: E402

REGION = ROOT / ".." / "AppData"  # placeholder; overwritten below
REGION = Path("C:/Users/nicho/AppData/Roaming/ModrinthApp/profiles/test/saves/Vandirtest10/region")
OUT = ROOT / "islands" / "_val"; OUT.mkdir(exist_ok=True)

PAIRS = {
    "01_clearings": [(75, 70), (76, 70)],
    "02_riparian":  [(68, 65), (68, 66)],
    "03_aspect":    [(89, 51), (89, 52)],
    "04_treeline":  [(57, 48), (58, 48)],
}
DEF = (120, 120, 120)


def tile_img(rx, rz):
    """512x512 RGB top-down of one region file (one tile)."""
    img = np.zeros((512, 512, 3), np.uint8); img[:] = (18, 28, 46)
    mca = REGION / f"r.{rx}.{rz}.mca"
    if not mca.exists():
        return img
    with open(mca, "rb") as f:
        for lz in range(32):
            for lx in range(32):
                try:
                    ch = read_chunk(f, lx, lz)
                except Exception:
                    ch = None
                if ch is None:
                    continue
                top = top_block_for_chunk(ch)             # (z,x) names
                for zz in range(16):
                    for xx in range(16):
                        nm = str(top[zz, xx]).replace("minecraft:", "")
                        if nm in ("air", "cave_air", "void_air"):
                            continue
                        img[lz * 16 + zz, lx * 16 + xx] = BLOCK_COLORS.get(nm, DEF)
    return img


def main():
    for fix, tiles in PAIRS.items():
        cols = [tile_img(rx, rz) for rx, rz in tiles]
        # 2x1 pair: stack horizontally if same rz (side by side), else vertically
        same_row = tiles[0][1] == tiles[1][1]
        combo = np.concatenate(cols, axis=1 if same_row else 0)
        im = Image.fromarray(combo)
        # label bar
        bar = Image.new("RGB", (im.width, 26), (10, 10, 14))
        d = ImageDraw.Draw(bar)
        d.text((6, 7), f"{fix}   tiles {tiles[0]} + {tiles[1]}   (each 512x512)", fill=(230, 230, 210))
        out = Image.new("RGB", (im.width, im.height + 26), (10, 10, 14))
        out.paste(bar, (0, 0)); out.paste(im, (0, 26))
        p = OUT / f"topdown_{fix}.png"
        out.save(p)
        print(f"wrote {p}")


if __name__ == "__main__":
    main()
