"""_eco_mask_previews_s101.py — topdown DESIGN PROOFS for the S101 DEM-derived
eco masks: biome+hillshade base (dimmed) with overlays —
  FLOODPLAIN teal, WINDTHROW orange, CLEARINGS yellow (alpha = strength).
Writes islands/_val/ecoprev_<name>.png; --upload posts to catbox + prints URLs.
"""
import subprocess
import sys
from pathlib import Path

import numpy as np

ISL = Path(__file__).resolve().parent
ROOT = ISL.parent
sys.path.insert(0, str(ROOT))

import importlib.util
_spec = importlib.util.spec_from_file_location("bp", str(ISL / "_bake_previews_s101.py"))
bp = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bp)

FLOOD_RGB = np.array([0, 215, 195], np.float32)
WIND_RGB = np.array([255, 130, 0], np.float32)
CLEAR_RGB = np.array([255, 232, 60], np.float32)


def eco_preview(d: Path, lut):
    import rasterio
    from PIL import Image
    need = ["override", "height", "wind_windthrow", "hydro_floodplain", "clearing_mask"]
    if not all((d / f"{n}.tif").exists() for n in need):
        print(f"  !! {d.name}: missing masks, skipped"); return None

    def rd(n):
        with rasterio.open(str(d / f"{n}.tif")) as s:
            return s.read(1)

    ov, h = rd("override"), rd("height").astype(np.float32)
    k = max(1, int(np.ceil(max(ov.shape) / 1400)))
    ov, h = ov[::k, ::k], h[::k, ::k]
    wt = rd("wind_windthrow")[::k, ::k].astype(np.float32) / 255.0
    fpm = rd("hydro_floodplain")[::k, ::k].astype(np.float32) / 255.0
    cl = rd("clearing_mask")[::k, ::k].astype(np.float32) / 255.0

    rgb = lut[np.clip(ov, 0, 255).astype(np.uint8)].astype(np.float32)
    land = h > bp.SEA_RAW
    rgb[~land] = np.array(bp.BIOME_COLORS.get("_OCEAN", (30, 80, 160)), np.float32)
    gz, gx = np.gradient(h / 60.0)
    shade = np.clip(0.72 + 0.28 * ((-gx - gz) / np.maximum(np.hypot(gx, gz), 1e-6))
                    * np.clip(np.hypot(gx, gz) * 3.0, 0, 1), 0.35, 1.15)
    img = rgb * np.where(land, shade, 0.9)[..., None]
    img *= 0.62                                             # dim base so overlays pop

    for m, col, a in ((fpm, FLOOD_RGB, 0.80), (wt, WIND_RGB, 0.85), (cl, CLEAR_RGB, 0.80)):
        alpha = (m * a)[..., None]
        img = img * (1 - alpha) + col[None, None, :] * alpha

    p = ISL / "_val" / f"ecoprev_{d.name}.png"
    Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).save(p)
    print(f"  wrote {p.name}")
    return p


def catbox(p: Path) -> str:
    r = subprocess.run(["curl", "-s", "-F", "reqtype=fileupload",
                        "-F", f"fileToUpload=@{p}", "https://catbox.moe/user/api.php"],
                       capture_output=True, text=True, timeout=120)
    return r.stdout.strip()


def main():
    upload = "--upload" in sys.argv
    lut = bp._lut()
    base = ISL / "masks_islands"
    outs = []
    for d in sorted(base.iterdir()):
        if d.is_dir() and not d.name.startswith("svg_"):
            p = eco_preview(d, lut)
            if p:
                outs.append(p)
    if upload:
        print("\ncatbox links:")
        for p in outs:
            print(f"  {p.stem.replace('ecoprev_', ''):42} {catbox(p)}")


if __name__ == "__main__":
    main()
