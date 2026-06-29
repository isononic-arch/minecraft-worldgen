"""_labeled_topdown.py <token> [outpath] — topdown of an island's written .mca with
a region grid + rx.rz tile labels (for the user to point at tiles to cull/erase)."""
import sys, json, re
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from islands.topdown_fast import read_chunk, top_rgb_for_chunk
ISL = ROOT / "islands"
tok = sys.argv[1]
out = sys.argv[2] if len(sys.argv) > 2 else str(ISL / "_val" / f"labeled_{tok}.png")


def _safe(n): return re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")


lay = json.loads((ISL / "layout.json").read_text())["islands"]
e = next(i for i in lay if tok in i["dem_path"] or tok in _safe(i["name"]))
name = _safe(e["name"]); odir = ISL / "out" / name
step = 6
regs = [tuple(map(int, m.stem.split('.')[1:3])) for m in odir.glob('r.*.mca')]
rxmin = min(r[0] for r in regs); rxmax = max(r[0] for r in regs)
rzmin = min(r[1] for r in regs); rzmax = max(r[1] for r in regs)
x0 = rxmin * 512; z0 = rzmin * 512
W = ((rxmax + 1) * 512 - x0) // step + 1
H = ((rzmax + 1) * 512 - z0) // step + 1
img = np.full((H, W, 3), (18, 28, 46), np.uint8)
for mca in sorted(odir.glob('r.*.mca')):
    rx, rz = map(int, mca.stem.split('.')[1:3])
    with open(mca, 'rb') as f:
        for lz in range(32):
            for lx in range(32):
                try: ch = read_chunk(f, lx, lz)
                except Exception: ch = None
                if ch is None: continue
                rgb, has, ty = top_rgb_for_chunk(ch)
                cwx = (rx * 32 + lx) * 16; cwz = (rz * 32 + lz) * 16
                for zz in range(0, 16, step):
                    for xx in range(0, 16, step):
                        if not has[zz, xx]: continue
                        oyp = (cwz + zz - z0) // step; oxp = (cwx + xx - x0) // step
                        if 0 <= oyp < H and 0 <= oxp < W: img[oyp, oxp] = rgb[zz, xx]
im = Image.fromarray(img); dr = ImageDraw.Draw(im)
try: fnt = ImageFont.truetype("arial.ttf", 12)
except Exception: fnt = ImageFont.load_default()
for rx in range(rxmin, rxmax + 2):
    xp = (rx * 512 - x0) // step; dr.line([(xp, 0), (xp, H)], fill=(255, 70, 70), width=1)
for rz in range(rzmin, rzmax + 2):
    zp = (rz * 512 - z0) // step; dr.line([(0, zp), (W, zp)], fill=(255, 70, 70), width=1)
for rx in range(rxmin, rxmax + 1):
    for rz in range(rzmin, rzmax + 1):
        dr.text(((rx * 512 - x0) // step + 2, (rz * 512 - z0) // step + 2), f"{rx}.{rz}",
                fill=(255, 255, 0), font=fnt)
im.save(out); print("saved", out, im.size)
