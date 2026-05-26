"""
diag_show_paint_at_tile.py — Render PNGs of the painted hydro_region.png
around problem tiles so we can visually see why the rasterization fails.

For each tile:
  - "zoomed" PNG: 8k crop centered on the tile, with the tile box outlined
    in red.  Paint shown in green (id=2 river) / blue (id=1 lake).
  - "context" PNG: wider area showing where painted polygons actually live.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np
from PIL import Image, ImageDraw

TILE = 512
WORLD = 50000
EIGHT_K = 8192
SCALE_50K_TO_8K = EIGHT_K / WORLD  # 0.16384


def render_paint_at_tile(tile_x: int, tile_z: int, hr_arr8k: np.ndarray,
                          out_dir: Path, zoom_8k_px: int = 200,
                          context_8k_px: int = 600) -> None:
    """Render two PNGs per tile:
       - paint_<x>_<z>_zoom.png:    tile-centred 8k crop, tile box outlined.
       - paint_<x>_<z>_context.png: wider 8k crop for context."""

    # Tile bounds in 50k
    col0_50k = tile_x * TILE
    row0_50k = tile_z * TILE
    col1_50k = col0_50k + TILE
    row1_50k = row0_50k + TILE
    # Same bounds in 8k
    col0_8k = int(col0_50k * SCALE_50K_TO_8K)
    row0_8k = int(row0_50k * SCALE_50K_TO_8K)
    col1_8k = int(col1_50k * SCALE_50K_TO_8K) + 1
    row1_8k = int(row1_50k * SCALE_50K_TO_8K) + 1

    print(f"\n[tile ({tile_x},{tile_z})] "
          f"50k window cols={col0_50k}..{col1_50k} rows={row0_50k}..{row1_50k}")
    print(f"                  8k  window cols={col0_8k}..{col1_8k} "
          f"rows={row0_8k}..{row1_8k}")

    # Centre of tile in 8k
    cx_8k = (col0_8k + col1_8k) // 2
    cy_8k = (row0_8k + row1_8k) // 2

    def render(crop_radius: int, suffix: str, scale: int = 4) -> None:
        x0 = max(0, cx_8k - crop_radius)
        y0 = max(0, cy_8k - crop_radius)
        x1 = min(EIGHT_K, cx_8k + crop_radius)
        y1 = min(EIGHT_K, cy_8k + crop_radius)

        slab = hr_arr8k[y0:y1, x0:x1]
        n_river = int((slab == 2).sum())
        n_lake = int((slab == 1).sum())
        print(f"  {suffix}: 8k crop {x1-x0}x{y1-y0}  "
              f"river_paint(id=2)={n_river}  lake_paint(id=1)={n_lake}")

        # Render as RGB: black bg, green river, blue lake
        rgb = np.zeros((slab.shape[0], slab.shape[1], 3), dtype=np.uint8)
        rgb[..., 0] = 30  # dim grey background
        rgb[..., 1] = 30
        rgb[..., 2] = 30
        rgb[slab == 2] = [50, 220, 50]   # green = river paint
        rgb[slab == 1] = [60, 120, 240]  # blue = lake paint

        img = Image.fromarray(rgb, mode="RGB")
        # Upscale for viewing
        img = img.resize(
            (img.width * scale, img.height * scale),
            Image.NEAREST,
        )

        # Draw tile box in red
        # Convert 8k-local coords to scaled pixel coords
        tile_x0_local = (col0_8k - x0) * scale
        tile_y0_local = (row0_8k - y0) * scale
        tile_x1_local = (col1_8k - x0) * scale
        tile_y1_local = (row1_8k - y0) * scale
        draw = ImageDraw.Draw(img)
        # Clip box to image bounds
        x0d = max(0, min(img.width - 1, tile_x0_local))
        y0d = max(0, min(img.height - 1, tile_y0_local))
        x1d = max(0, min(img.width - 1, tile_x1_local))
        y1d = max(0, min(img.height - 1, tile_y1_local))
        draw.rectangle([x0d, y0d, x1d, y1d], outline=(255, 60, 60), width=2)

        # Annotate tile coord
        draw.text((4, 4), f"tile ({tile_x},{tile_z})  "
                          f"{suffix}  river={n_river}  lake={n_lake}",
                  fill=(255, 255, 255))

        path = out_dir / f"paint_{tile_x}_{tile_z}_{suffix}.png"
        img.save(path)
        print(f"  -> {path}")

    out_dir.mkdir(parents=True, exist_ok=True)
    render(crop_radius=zoom_8k_px // 2, suffix="zoom", scale=4)
    render(crop_radius=context_8k_px // 2, suffix="context", scale=2)


def main() -> int:
    hr_path = Path("masks/hydro_region.png")
    if not hr_path.exists():
        print(f"hydro_region.png not found at {hr_path}")
        return 1
    print(f"Loading hydro_region.png ({hr_path.stat().st_size / 1024:.0f} KB)")
    hr_img = Image.open(hr_path).convert("L")
    hr_arr = np.asarray(hr_img, dtype=np.uint8)
    print(f"Paint mask: {hr_arr.shape} dtype={hr_arr.dtype}")
    n_river_total = int((hr_arr == 2).sum())
    n_lake_total = int((hr_arr == 1).sum())
    print(f"Totals: river_paint(id=2)={n_river_total}  "
          f"lake_paint(id=1)={n_lake_total}")

    out_dir = Path("diag_paint_at_tile/")
    out_dir.mkdir(exist_ok=True)

    for tx, tz in [(13, 82), (51, 53), (33, 7), (60, 69)]:
        render_paint_at_tile(tx, tz, hr_arr, out_dir,
                              zoom_8k_px=120, context_8k_px=400)

    # Also dump a full-painted-mask overview
    full = np.zeros((hr_arr.shape[0], hr_arr.shape[1], 3), dtype=np.uint8)
    full[..., 0] = 30
    full[..., 1] = 30
    full[..., 2] = 30
    full[hr_arr == 2] = [50, 220, 50]
    full[hr_arr == 1] = [60, 120, 240]
    # mark each test tile with a red rectangle
    full_img = Image.fromarray(full, mode="RGB")
    draw = ImageDraw.Draw(full_img)
    for tx, tz in [(13, 82), (51, 53), (33, 7), (60, 69)]:
        c0 = int(tx * TILE * SCALE_50K_TO_8K)
        r0 = int(tz * TILE * SCALE_50K_TO_8K)
        c1 = int((tx + 1) * TILE * SCALE_50K_TO_8K)
        r1 = int((tz + 1) * TILE * SCALE_50K_TO_8K)
        draw.rectangle([c0, r0, c1, r1],
                        outline=(255, 60, 60), width=2)
        draw.text((c0 + 2, r0 + 2), f"({tx},{tz})", fill=(255, 255, 255))
    # Downscale to 4k for viewing
    overview = full_img.resize((4096, 4096), Image.LANCZOS)
    overview.save(out_dir / "paint_overview_world.png")
    print(f"\n-> {out_dir/'paint_overview_world.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
