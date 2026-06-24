"""islands/tint_set_map.py — world overview showing which islands would get the
tropical MC-biome tint override (jungle grass + warm_ocean ring). Mainland dark,
tint-set islands BRIGHT GREEN, excluded (cold/Irish) islands AMBER.
"""
import sys, json
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from islands.render_islands import safe_name, _key

ISL = ROOT / "islands"
MASKS_OUT = ISL / "masks_islands"
TARGET = 1900
EXCLUDE = {"17_288", "49_722", "-50_393", "-17_622"}   # St Kitts (Irish), Fogo (boreal), Madre (sub-polar), Efate (W outlier, keeps own tint)

OCEAN = (10, 13, 22); MAINLAND = (44, 50, 62)
TINT = (70, 210, 95); NOTINT = (215, 150, 55)


def _snap(v): return int(round(v / 512) * 512)


def main():
    lay = json.loads((ISL / "layout.json").read_text())["islands"]
    pieces = [(0, 0, ROOT / "masks" / "override.tif", 50000, 50000, "MAINLAND", None)]
    for e in lay:
        d = MASKS_OUT / safe_name(e["name"])
        if not (d / "override.tif").exists() or not (d / "manifest.json").exists():
            continue
        man = json.loads((d / "manifest.json").read_text())
        ox, oz = _snap(man["world_offset_px"][0]), _snap(man["world_offset_px"][1])
        H, W = man["world_hw"]
        pieces.append((ox, oz, d / "override.tif", W, H, _key(e["dem_path"]), e["name"]))

    minx = min(p[0] for p in pieces); maxx = max(p[0] + p[3] for p in pieces)
    minz = min(p[1] for p in pieces); maxz = max(p[1] + p[4] for p in pieces)
    scale = max(maxx - minx, maxz - minz) / TARGET
    cw, ch = int((maxx - minx) / scale) + 1, int((maxz - minz) / scale) + 1
    canvas = np.zeros((ch, cw, 3), np.uint8); canvas[:] = OCEAN
    labels = []

    for ox, oz, path, W, H, key, name in pieces:
        dw, dh = max(1, int(W / scale)), max(1, int(H / scale))
        try:
            ov = rasterio.open(path).read(1, out_shape=(dh, dw), resampling=Resampling.nearest)
        except Exception as ex:
            print(f"  skip {key}: {ex}"); continue
        land = ov > 0
        if key == "MAINLAND":
            color = MAINLAND
        else:
            color = NOTINT if key in EXCLUDE else TINT
        px0, pz0 = int((ox - minx) / scale), int((oz - minz) / scale)
        ez, ex = min(pz0 + dh, ch), min(px0 + dw, cw)
        reg = canvas[pz0:ez, px0:ex]; m = land[:ez - pz0, :ex - px0]
        reg[m] = color
        if key != "MAINLAND" and land.any():
            ys, xs = np.where(land)
            labels.append((px0 + int(xs.mean()), pz0 + int(ys.mean()), key, key not in EXCLUDE))
        print(f"  {key:9s} {'TINT' if (key!='MAINLAND' and key not in EXCLUDE) else ('--' if key=='MAINLAND' else 'EXCLUDE'):7s} "
              f"world({ox},{oz})  {name or ''}")

    img = Image.fromarray(canvas)
    dr = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 17)
    except Exception:
        font = ImageFont.load_default()
    for x, y, key, is_tint in labels:
        col = (160, 255, 180) if is_tint else (255, 200, 120)
        dr.text((x + 6, y - 8), key, fill=col, font=font, stroke_width=2, stroke_fill=(0, 0, 0))
    # mainland label
    dr.text((int((0 - minx) / scale) + 20, int((0 - minz) / scale) + 20), "MAINLAND (Vandir)",
            fill=(150, 160, 180), font=font, stroke_width=2, stroke_fill=(0, 0, 0))
    out = ISL / "out" / "tint_set_map.png"
    img.save(out)
    print(f"\nGREEN = jungle grass + warm_ocean ring   AMBER = excluded (keep own tint)")
    print(f"saved {out}  ({cw}x{ch})")


if __name__ == "__main__":
    main()
