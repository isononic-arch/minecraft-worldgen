"""
diag_terrain_derived_composite.py — Render a composite PNG showing how the
4 S88 masks stack visually in-game ORDER, over the rock_gap base.

Stack order (bottom → top, last on top):
  1. Height backdrop          dim grayscale  (geographic context)
  2. Rock-gap base (slope≥35°) light gray solid  (existing rock_gap mask)
  3. Bedrock drainage         red-orange  (water-cut channels)
  4. Talus apron              yellow  (rubble at cliff bases)
  5. Cliff cap                green  (resistant cap at cliff tops)

Aspect is NOT in the stack — it modulates rock_gap probability earlier in
the pipeline, never paints a block.  Shown as a separate side reference panel.

Painting model: each subsequent layer OVERWRITES previous pixels where
intensity > threshold.  No additive blending → no muddy mixed colors.

Output:
  diag_terrain_derived/composite_in_game_stack.png   (2048x2048)
  diag_terrain_derived/composite_in_game_stack_legend.png  (annotated)

Runtime: ~30-60 s.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np
import rasterio
from rasterio.enums import Resampling
from PIL import Image, ImageDraw

WORLD_50K = 50_000
THUMB_SIZE = 2048
TILE = 512

REF_TILES = [
    (36, 15, "karst-cliff"),
    (24, 80, "desert-rock"),
    (51, 53, "lakes-floodplain"),
    (13, 82, "RFC-coastal"),
    (89, 52, "cliff-litho"),
]

# Layer colors — chosen for high contrast on each other and the gray backdrop
COL_ROCKGAP   = (140, 140, 140)   # light gray
COL_BEDROCK   = (255,  80,  30)   # red-orange
COL_TALUS     = (240, 200,  60)   # warm yellow
COL_CLIFFCAP  = ( 70, 230,  90)   # bright green

# Intensity threshold to paint each layer (matches config intensity_threshold)
THRESH = 64


def read_thumb_avg(masks_dir: Path, name: str,
                    out_size: int = THUMB_SIZE) -> np.ndarray:
    path = masks_dir / f"{name}.tif"
    with rasterio.open(str(path)) as src:
        return src.read(1, out_shape=(out_size, out_size),
                        resampling=Resampling.average)


def read_thumb_block_max(masks_dir: Path, name: str,
                          out_size: int = THUMB_SIZE) -> np.ndarray:
    HI_RES = out_size * 4
    path = masks_dir / f"{name}.tif"
    with rasterio.open(str(path)) as src:
        hi = src.read(1, out_shape=(HI_RES, HI_RES),
                       resampling=Resampling.nearest)
    f = HI_RES // out_size
    return hi.reshape(out_size, f, out_size, f).max(axis=(1, 3))


def compute_slope_thumb(masks_dir: Path,
                         out_size: int = THUMB_SIZE) -> np.ndarray:
    """Compute slope_deg at thumbnail resolution from height + spline LUT.
    1 pixel = (50000 / out_size) world blocks.  Divide gradient mag by
    that factor to recover true rise/run."""
    from core import column_generator as col_gen
    from scipy.ndimage import gaussian_filter
    h_raw = read_thumb_avg(masks_dir, "height", out_size)
    h_norm = (h_raw.astype(np.float32) /
              (65535.0 if h_raw.dtype == np.uint16 else 1.0))
    h_int = np.clip((h_norm * 65535.0).astype(np.int32), 0, 65535)
    sy = col_gen._LUT[h_int].astype(np.int16)
    sy_smooth = gaussian_filter(sy.astype(np.float32), sigma=1.5)
    gy, gx = np.gradient(sy_smooth)
    scale = WORLD_50K / out_size  # blocks per pixel
    return np.degrees(np.arctan(np.hypot(gx, gy) / scale)).astype(np.float32)


def render_composite() -> None:
    masks_dir = Path("masks/")
    out_dir = Path("diag_terrain_derived/")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Computing slope_deg at thumbnail res (for rock_gap base)...")
    slope_deg = compute_slope_thumb(masks_dir)
    rock_gap_base = slope_deg >= 35.0
    print(f"  rock_gap pixels: {int(rock_gap_base.sum())}")

    print("Reading height backdrop...")
    h_raw = read_thumb_avg(masks_dir, "height")
    if h_raw.dtype == np.uint16:
        h_norm = h_raw.astype(np.float32) / 65535.0
    else:
        h_norm = h_raw.astype(np.float32)
    # Dim backdrop so layer colors pop
    backdrop_v = np.clip(h_norm * 0.5 + 0.05, 0, 0.55)  # 0.05..0.55
    backdrop = np.stack([
        (backdrop_v * 60).astype(np.uint8),
        (backdrop_v * 60).astype(np.uint8),
        (backdrop_v * 70).astype(np.uint8),  # tiny blue tint for ocean
    ], axis=-1)

    print("Loading 3 stacked masks (bedrock, talus, cap)...")
    bedrock = read_thumb_block_max(masks_dir, "bedrock_drainage")
    talus   = read_thumb_block_max(masks_dir, "talus_apron")
    cap     = read_thumb_block_max(masks_dir, "cliff_cap")

    # ---- Build composite, paint-on layer by layer ----
    rgb = backdrop.copy()

    # Layer 1: rock_gap base — light gray
    print(f"Layer 1: rock_gap base (slope >= 35deg) - {int(rock_gap_base.sum())} px")
    rgb[rock_gap_base] = COL_ROCKGAP

    # Layer 2: bedrock_drainage — red-orange over rock_gap
    bedrock_paint = bedrock > THRESH
    print(f"Layer 2: bedrock_drainage   - {int(bedrock_paint.sum())} px"
          f" (over rock_gap: {int((bedrock_paint & rock_gap_base).sum())})")
    rgb[bedrock_paint] = COL_BEDROCK

    # Layer 3: talus_apron — yellow over rock_gap/bedrock
    talus_paint = talus > THRESH
    print(f"Layer 3: talus_apron        - {int(talus_paint.sum())} px"
          f" (already painted: {int((talus_paint & (bedrock_paint | rock_gap_base)).sum())})")
    rgb[talus_paint] = COL_TALUS

    # Layer 4: cliff_cap — green, paints last (top)
    cap_paint = cap > THRESH
    print(f"Layer 4: cliff_cap          - {int(cap_paint.sum())} px"
          f" (already painted: {int((cap_paint & (bedrock_paint | talus_paint | rock_gap_base)).sum())})")
    rgb[cap_paint] = COL_CLIFFCAP

    img = Image.fromarray(rgb, mode="RGB")

    # Draw tile-reference outlines
    draw = ImageDraw.Draw(img)
    scale = THUMB_SIZE / WORLD_50K
    for tx, tz, label in REF_TILES:
        x0 = tx * TILE * scale
        y0 = tz * TILE * scale
        x1 = (tx + 1) * TILE * scale
        y1 = (tz + 1) * TILE * scale
        draw.rectangle([x0, y0, x1, y1], outline=(255, 60, 200), width=2)
        draw.text((x0 + 4, y0 + 2), f"({tx},{tz}) {label}",
                  fill=(255, 200, 240))

    plain_path = out_dir / "composite_in_game_stack.png"
    img.save(plain_path)
    print(f"  -> {plain_path}")

    # ---- Annotated version with legend bar ----
    LEGEND_H = 96
    annotated = Image.new("RGB",
                           (THUMB_SIZE, THUMB_SIZE + LEGEND_H),
                           (20, 20, 20))
    annotated.paste(img, (0, 0))
    d2 = ImageDraw.Draw(annotated)
    legend_y = THUMB_SIZE + 18
    swatches = [
        (COL_ROCKGAP, "1. rock_gap base (slope ≥ 35°)"),
        (COL_BEDROCK, "2. bedrock_drainage (water-cut)"),
        (COL_TALUS,   "3. talus_apron (rubble below)"),
        (COL_CLIFFCAP,"4. cliff_cap (cap above)"),
    ]
    x_off = 16
    for col, label in swatches:
        d2.rectangle([x_off, legend_y, x_off + 28, legend_y + 28],
                      fill=col, outline=(255, 255, 255))
        d2.text((x_off + 36, legend_y + 6), label, fill=(240, 240, 240))
        x_off += 380
    d2.text((16, legend_y + 50),
             "Paint order (last = on top).  Pink boxes = reference tiles.  "
             "Aspect not shown (it modulates rock_gap probability, not blocks).",
             fill=(180, 180, 180))

    leg_path = out_dir / "composite_in_game_stack_legend.png"
    annotated.save(leg_path)
    print(f"  -> {leg_path}")


def main() -> int:
    render_composite()
    return 0


if __name__ == "__main__":
    sys.exit(main())
