"""
Generate colored thumbnails of every override PNG variant, plus a single
6-panel comparison grid for easy side-by-side viewing.
"""
from __future__ import annotations
import numpy as np
from PIL import Image
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT  = REPO / "memory"

ZONE_RGB = {
    0:   (30, 80, 160),    # Ocean
    10:  (180, 200, 140), 20:  (30, 120, 60), 30:  (60, 130, 90),
    35:  (180, 200, 220), 40:  (150, 170, 200), 50:  (220, 230, 240),
    55:  (240, 245, 255), 60:  (80, 160, 80),  70:  (20, 160, 80),
    80:  (60, 140, 100),  # RIPARIAN_WOODLAND
    90:  (190, 160, 80),  100: (180, 170, 150), 110: (160, 200, 140),
    115: (120, 180, 130), 120: (60, 140, 70),  130: (200, 180, 100),
    140: (140, 160, 100), 150: (180, 160, 120), 160: (20, 140, 80),
    170: (230, 200, 120), 190: (210, 185, 120), 200: (200, 170, 110),
    210: (170, 160, 100), 220: (40, 150, 100), 230: (50, 140, 90),
    240: (80, 150, 130),  # FRESHWATER_FEN
}

FILES = [
    ("override_final.png",                                     "CURRENT MASTER (S62 patched)"),
    ("override_vectorized.png",                                "CURRENT VECTORIZED (S62 patched)"),
    ("override_final_pre_s62.png",                             "pre-S62 backup (today)"),
    ("override_vectorized_pre_s62.png",                        "pre-S62 vectorized backup"),
    ("override_final_backup.png",                              "override_final_backup (Mar 1)"),
    ("_stale/override_pngs/override_smoothed.png",             "STALE override_smoothed (Mar 6)"),
]

def zones_to_rgb(zones):
    rgb = np.zeros((*zones.shape, 3), dtype=np.uint8)
    for z, c in ZONE_RGB.items():
        rgb[zones == z] = c
    return rgb

THUMB_SIZE = 1024   # each panel
panels = []
for fname, label in FILES:
    path = REPO / fname
    if not path.exists():
        print(f"  MISSING: {fname}")
        continue
    arr = np.array(Image.open(path))
    if arr.ndim == 3:
        arr = arr[..., 0]
    rgb = zones_to_rgb(arr)
    img = Image.fromarray(rgb)
    img_thumb = img.resize((THUMB_SIZE, THUMB_SIZE), Image.Resampling.NEAREST)
    # Also save individual thumb
    thumb_path = OUT / f"s62_compare_{path.stem}.jpg"
    img_thumb.save(thumb_path, quality=85)
    print(f"  {fname} -> {thumb_path.name}  ({arr.shape}, zones={len(np.unique(arr))})")
    # Add label bar under thumb
    from PIL import ImageDraw, ImageFont
    LABEL_H = 60
    panel = Image.new('RGB', (THUMB_SIZE, THUMB_SIZE + LABEL_H), (255, 255, 255))
    panel.paste(img_thumb, (0, 0))
    draw = ImageDraw.Draw(panel)
    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except:
        font = ImageFont.load_default()
    # Zone count as subtitle
    zones = np.unique(arr)
    sub = f"zones={len(zones)} | 80={int((arr==80).sum())} 240={int((arr==240).sum())}"
    draw.text((10, THUMB_SIZE + 8), label, fill=(0, 0, 0), font=font)
    try:
        font2 = ImageFont.truetype("arial.ttf", 20)
    except:
        font2 = ImageFont.load_default()
    draw.text((10, THUMB_SIZE + 38), sub, fill=(80, 80, 80), font=font2)
    panels.append(panel)

# 2-column grid
if panels:
    cols = 2
    rows = (len(panels) + cols - 1) // cols
    GAP = 20
    grid_w = cols * THUMB_SIZE + (cols + 1) * GAP
    panel_h = panels[0].height
    grid_h = rows * panel_h + (rows + 1) * GAP
    grid = Image.new('RGB', (grid_w, grid_h), (250, 250, 250))
    for i, p in enumerate(panels):
        r, c = i // cols, i % cols
        x = GAP + c * (THUMB_SIZE + GAP)
        y = GAP + r * (panel_h + GAP)
        grid.paste(p, (x, y))
    out_grid = OUT / "s62_override_comparison.jpg"
    # Downscale the grid for easier viewing
    target_w = 2200
    ratio = target_w / grid_w
    grid_small = grid.resize((int(grid_w * ratio), int(grid_h * ratio)), Image.Resampling.LANCZOS)
    grid_small.save(out_grid, quality=88, optimize=True)
    print(f"\nComparison grid: {out_grid}")
    print(f"  size: {grid_small.size}")
