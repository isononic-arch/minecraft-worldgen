"""world_map.py — composite the MAINLAND + all baked ISLANDS into one biome-color
map at true world coordinates, spanning NE-most to SW-most piece. Low-RAM
(rasterio out_shape downsampled reads). Output islands/out/world_map.png.
"""
import sys, json, glob
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.biome_assignment import OVERRIDE_BIOME_MAP
from tools.world_biome_map import BIOME_COLORS

TARGET = 1700           # max canvas dim (px)
OCEAN = (18, 28, 46)


def main():
    pieces = []   # (world_x0, world_z0, override_path, W, H, label)
    ml = ROOT / "masks" / "override.tif"
    if ml.exists():
        pieces.append((0, 0, str(ml), 50000, 50000, "MAINLAND"))
    for m in sorted(glob.glob(str(ROOT / "islands" / "masks_islands" / "*" / "manifest.json"))):
        man = json.loads(Path(m).read_text()); d = Path(m).parent
        if not (d / "override.tif").exists():
            continue
        ox, oz = man["world_offset_px"]
        sx, sz = round(ox / 512) * 512, round(oz / 512) * 512
        H, W = man["world_hw"]
        pieces.append((sx, sz, str(d / "override.tif"), W, H, d.name[:18]))
    if not pieces:
        print("no pieces"); return

    minx = min(p[0] for p in pieces); maxx = max(p[0] + p[3] for p in pieces)
    minz = min(p[1] for p in pieces); maxz = max(p[1] + p[4] for p in pieces)
    scale = max(maxx - minx, maxz - minz) / TARGET
    cw = int((maxx - minx) / scale) + 1
    ch = int((maxz - minz) / scale) + 1
    canvas = np.zeros((ch, cw, 3), np.uint8); canvas[:] = OCEAN
    lut = np.zeros((256, 3), np.uint8)
    for z, n in OVERRIDE_BIOME_MAP.items():
        lut[z] = BIOME_COLORS.get(n, (150, 150, 150))

    print(f"world bounds X[{minx},{maxx}] Z[{minz},{maxz}]  scale 1:{scale:.0f}  canvas {cw}x{ch}")
    for sx, sz, path, W, H, label in pieces:
        dw = max(1, int(W / scale)); dh = max(1, int(H / scale))
        try:
            ov = rasterio.open(path).read(1, out_shape=(dh, dw), resampling=Resampling.nearest)
        except Exception as e:
            print(f"  skip {label}: {e}"); continue
        px0 = int((sx - minx) / scale); pz0 = int((sz - minz) / scale)
        sub = lut[ov]; mask = ov > 0
        ph, pw = sub.shape[:2]
        ez, ex = min(pz0 + ph, ch), min(px0 + pw, cw)
        if ez <= pz0 or ex <= px0:
            continue
        reg = canvas[pz0:ez, px0:ex]
        m2 = mask[:ez - pz0, :ex - px0]
        reg[m2] = sub[:ez - pz0, :ex - px0][m2]
        print(f"  placed {label:20} at world ({sx},{sz}) -> px ({px0},{pz0})  {int(mask.sum())} land px")
    out = ROOT / "islands" / "out" / "world_map.png"
    Image.fromarray(canvas).save(out)
    print(f"saved {out}  ({cw}x{ch})")


if __name__ == "__main__":
    main()
