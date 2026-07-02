"""_bake_previews_s101.py — per-island MASK PREVIEW renders for the S101 bake.

For every islands/masks_islands/<name>/: biome-colored override (canonical
BIOME_COLORS via OVERRIDE_BIOME_MAP) x height hillshade, beach(9-band) shown via
the override paint itself, ocean = dark blue. Downsampled to <=1400px longest
side. Writes islands/_val/bakeprev_<name>.png. --upload also posts each PNG to
catbox.moe and prints the URLs (user-requested share flow).
"""
import sys, subprocess
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.biome_assignment import OVERRIDE_BIOME_MAP           # zone -> name
from tools.world_biome_map import BIOME_COLORS                 # name -> RGB

SEA_RAW = 17050
OUT = ROOT / "islands" / "_val"
OUT.mkdir(exist_ok=True)


def _lut():
    lut = np.zeros((256, 3), np.uint8)
    lut[:] = BIOME_COLORS.get("_OCEAN", (30, 80, 160))
    for code, name in OVERRIDE_BIOME_MAP.items():
        if name and name in BIOME_COLORS:
            lut[code] = BIOME_COLORS[name]
    return lut


def preview(d: Path, lut) -> Path | None:
    import rasterio
    ov_p, h_p = d / "override.tif", d / "height.tif"
    if not ov_p.exists() or not h_p.exists():
        print(f"  !! {d.name}: missing override/height, skipped")
        return None
    with rasterio.open(ov_p) as s:
        ov = s.read(1)
    with rasterio.open(h_p) as s:
        h = s.read(1).astype(np.float32)

    k = max(1, int(np.ceil(max(ov.shape) / 1400)))
    ov, h = ov[::k, ::k], h[::k, ::k]

    rgb = lut[np.clip(ov, 0, 255).astype(np.uint8)].astype(np.float32)
    land = h > SEA_RAW
    rgb[~land] = np.array(BIOME_COLORS.get("_OCEAN", (30, 80, 160)), np.float32)

    # hillshade (NW light) on land; flat ocean gets a mild depth shade
    gz, gx = np.gradient(h / 60.0)
    shade = np.clip(0.72 + 0.28 * ((-gx - gz) / np.maximum(np.hypot(gx, gz), 1e-6))
                    * np.clip(np.hypot(gx, gz) * 3.0, 0, 1), 0.35, 1.15)
    depth = np.clip((h - h[~land].min() if (~land).any() else h) /
                    max(float(SEA_RAW - (h[~land].min() if (~land).any() else 0)), 1), 0.55, 1.0)
    mul = np.where(land, shade, depth * 0.9)
    img = np.clip(rgb * mul[..., None], 0, 255).astype(np.uint8)

    from PIL import Image
    p = OUT / f"bakeprev_{d.name}.png"
    Image.fromarray(img).save(p)
    print(f"  wrote {p.name}  {img.shape[1]}x{img.shape[0]} (1:{k})")
    return p


def catbox(p: Path) -> str:
    r = subprocess.run(["curl", "-s", "-F", "reqtype=fileupload",
                        "-F", f"fileToUpload=@{p}",
                        "https://catbox.moe/user/api.php"],
                       capture_output=True, text=True, timeout=120)
    return r.stdout.strip()


def main():
    upload = "--upload" in sys.argv
    lut = _lut()
    base = ROOT / "islands" / "masks_islands"
    pngs = []
    for d in sorted(base.iterdir()):
        if d.is_dir() and not d.name.startswith(("svg_",)):
            p = preview(d, lut)
            if p:
                pngs.append(p)
    if upload:
        print("\ncatbox links:")
        for p in pngs:
            url = catbox(p)
            print(f"  {p.stem.replace('bakeprev_', ''):40} {url}")


if __name__ == "__main__":
    main()
