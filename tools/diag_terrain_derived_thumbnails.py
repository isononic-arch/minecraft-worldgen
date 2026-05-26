"""
diag_terrain_derived_thumbnails.py — Render eyeball-able PNG thumbnails of
the 4 S88 precompute masks (aspect, bedrock_drainage, talus_apron, cliff_cap).

Each mask is downsampled to 2048x2048 via rasterio out_shape (fast, no full
50k load), then composed against a faint grayscale heightmap backdrop so the
mask features are visible in geographic context.  Each thumbnail has a few
reference tile outlines for spatial bearings.

Outputs:
  diag_terrain_derived/aspect.png
  diag_terrain_derived/bedrock_drainage.png
  diag_terrain_derived/talus_apron.png
  diag_terrain_derived/cliff_cap.png
  diag_terrain_derived/overview_4panel.png   (all 4 in one grid)

Runtime: ~1-2 min.  Pure read + Pillow render.  No mask writes.
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

# Reference tiles to outline on each thumbnail
REF_TILES = [
    (36, 15, "karst-cliff"),     # S87 walk #4 reference
    (24, 80, "desert-rock"),     # rock palette reference
    (51, 53, "lakes-floodplain"),
    (13, 82, "RFC-coastal"),
    (89, 52, "cliff-litho"),
]


def read_thumb(masks_dir: Path, name: str,
                resampling: Resampling = Resampling.average) -> np.ndarray:
    """Read a 50k TIF downsampled to THUMB_SIZE."""
    path = masks_dir / f"{name}.tif"
    with rasterio.open(str(path)) as src:
        return src.read(
            1, out_shape=(THUMB_SIZE, THUMB_SIZE),
            resampling=resampling,
        )


def read_thumb_block_max(masks_dir: Path, name: str) -> np.ndarray:
    """Read a 50k TIF at 4x THUMB_SIZE via nearest, then take per-block max
    down to THUMB_SIZE.  Preserves sparse intensity peaks (rasterio's
    `Resampling.max` mode isn't available on read paths)."""
    HI_RES = THUMB_SIZE * 4  # 8192 — quarter of 50k, ~6 block precision
    path = masks_dir / f"{name}.tif"
    with rasterio.open(str(path)) as src:
        hi = src.read(
            1, out_shape=(HI_RES, HI_RES),
            resampling=Resampling.nearest,
        )
    # Per-block max from HI_RES → THUMB_SIZE (factor 4 in each axis)
    f = HI_RES // THUMB_SIZE
    # Reshape (THUMB, f, THUMB, f) → max over axis 1 and 3
    reshaped = hi.reshape(THUMB_SIZE, f, THUMB_SIZE, f)
    return reshaped.max(axis=(1, 3))


def hillshade_backdrop(masks_dir: Path) -> np.ndarray:
    """Return RGB (THUMB_SIZE, THUMB_SIZE, 3) faint grayscale heightmap for
    use as a backdrop behind the mask overlays."""
    h = read_thumb(masks_dir, "height", Resampling.average)
    if h.dtype == np.uint16:
        h = h.astype(np.float32) / 65535.0
    else:
        h = h.astype(np.float32)
    # Stretch to grayscale, dim it so overlays pop
    g = np.clip(h * 0.8 + 0.1, 0, 1)  # range [0.1, 0.9]
    g8 = (g * 80).astype(np.uint8)  # dim to ~30% brightness max
    return np.stack([g8, g8, g8], axis=-1)


def colorize_aspect(aspect: np.ndarray) -> np.ndarray:
    """Cyclic HSV colormap for compass direction.  255 sentinel = dark gray."""
    # Map 0..254 → hue 0..1 (cyclic).  255 = sentinel.
    sentinel = aspect == 255
    hue = (aspect.astype(np.float32) / 255.0) % 1.0
    sat = np.full_like(hue, 0.85)
    val = np.full_like(hue, 0.85)
    # HSV → RGB conversion (vectorised)
    h_i = (hue * 6).astype(np.int32) % 6
    f = (hue * 6) - h_i
    p = val * (1 - sat)
    q = val * (1 - f * sat)
    t = val * (1 - (1 - f) * sat)
    rgb = np.zeros((aspect.shape[0], aspect.shape[1], 3), dtype=np.float32)
    masks_h = [h_i == k for k in range(6)]
    for k, (r, g, b) in enumerate([
        (val, t, p), (q, val, p), (p, val, t),
        (p, q, val), (t, p, val), (val, p, q),
    ]):
        rgb[masks_h[k]] = np.stack([
            r[masks_h[k]], g[masks_h[k]], b[masks_h[k]]
        ], axis=-1)
    rgb_u8 = (rgb * 255).astype(np.uint8)
    rgb_u8[sentinel] = [40, 40, 40]  # dark gray sentinel
    return rgb_u8


def colorize_intensity(arr: np.ndarray, base_color: tuple[int, int, int],
                        backdrop: np.ndarray) -> np.ndarray:
    """Composite a uint8 intensity mask over a backdrop using `base_color`
    at max intensity, faded to backdrop at intensity=0."""
    intensity = arr.astype(np.float32) / 255.0  # 0..1
    color = np.array(base_color, dtype=np.float32)
    overlay = intensity[..., None] * color[None, None, :]
    backdrop_f = backdrop.astype(np.float32)
    # Composite: alpha = intensity, blend with backdrop
    alpha = intensity[..., None]
    out = backdrop_f * (1 - alpha) + overlay
    return np.clip(out, 0, 255).astype(np.uint8)


def draw_ref_tile_outlines(img: Image.Image,
                            ref_tiles: list[tuple[int, int, str]]) -> None:
    """Draw red rectangles + labels for reference tiles."""
    draw = ImageDraw.Draw(img)
    scale = THUMB_SIZE / WORLD_50K
    for tx, tz, label in ref_tiles:
        x0 = tx * TILE * scale
        y0 = tz * TILE * scale
        x1 = (tx + 1) * TILE * scale
        y1 = (tz + 1) * TILE * scale
        draw.rectangle([x0, y0, x1, y1], outline=(255, 60, 60), width=2)
        draw.text((x0 + 4, y0 + 2), f"({tx},{tz}) {label}",
                  fill=(255, 220, 220))


def render_all(masks_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    print("Loading height backdrop...")
    backdrop = hillshade_backdrop(masks_dir)

    panels: list[tuple[str, np.ndarray]] = []

    # --- Aspect ---
    print("Rendering aspect.png...")
    aspect = read_thumb(masks_dir, "aspect", Resampling.nearest)
    aspect_rgb = colorize_aspect(aspect)
    # Faint backdrop blend so the compass-colored aspect dominates
    composite = (aspect_rgb.astype(np.float32) * 0.85 +
                 backdrop.astype(np.float32) * 0.15)
    aspect_img = Image.fromarray(np.clip(composite, 0, 255).astype(np.uint8),
                                  mode="RGB")
    draw_ref_tile_outlines(aspect_img, REF_TILES)
    aspect_img.save(out_dir / "aspect.png")
    panels.append(("aspect", np.asarray(aspect_img)))

    # --- Bedrock drainage (red/orange) ---
    print("Rendering bedrock_drainage.png...")
    bdr = read_thumb_block_max(masks_dir, "bedrock_drainage")
    bdr_rgb = colorize_intensity(bdr, base_color=(255, 80, 30), backdrop=backdrop)
    bdr_img = Image.fromarray(bdr_rgb, mode="RGB")
    draw_ref_tile_outlines(bdr_img, REF_TILES)
    bdr_img.save(out_dir / "bedrock_drainage.png")
    panels.append(("bedrock_drainage", np.asarray(bdr_img)))

    # --- Talus apron (yellow) ---
    print("Rendering talus_apron.png...")
    talus = read_thumb_block_max(masks_dir, "talus_apron")
    talus_rgb = colorize_intensity(talus, base_color=(255, 200, 60), backdrop=backdrop)
    talus_img = Image.fromarray(talus_rgb, mode="RGB")
    draw_ref_tile_outlines(talus_img, REF_TILES)
    talus_img.save(out_dir / "talus_apron.png")
    panels.append(("talus_apron", np.asarray(talus_img)))

    # --- Cliff cap (green) ---
    print("Rendering cliff_cap.png...")
    cap = read_thumb_block_max(masks_dir, "cliff_cap")
    cap_rgb = colorize_intensity(cap, base_color=(80, 230, 80), backdrop=backdrop)
    cap_img = Image.fromarray(cap_rgb, mode="RGB")
    draw_ref_tile_outlines(cap_img, REF_TILES)
    cap_img.save(out_dir / "cliff_cap.png")
    panels.append(("cliff_cap", np.asarray(cap_img)))

    # --- 4-panel overview ---
    print("Composing overview_4panel.png...")
    half = THUMB_SIZE // 2
    panel_size = half
    overview = Image.new("RGB", (THUMB_SIZE, THUMB_SIZE), (0, 0, 0))
    positions = [(0, 0), (half, 0), (0, half), (half, half)]
    for (label, rgb), (x, y) in zip(panels, positions):
        small = Image.fromarray(rgb, mode="RGB").resize(
            (panel_size, panel_size), Image.LANCZOS
        )
        overview.paste(small, (x, y))
    draw = ImageDraw.Draw(overview)
    for (label, _), (x, y) in zip(panels, positions):
        draw.text((x + 8, y + 8), label, fill=(255, 255, 255))
    overview.save(out_dir / "overview_4panel.png")
    print(f"  -> {out_dir / 'overview_4panel.png'}")


def main() -> int:
    masks_dir = Path("masks/")
    out_dir = Path("diag_terrain_derived/")
    render_all(masks_dir, out_dir)
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
