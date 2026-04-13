"""diag_cliff_crosssection.py — Vertical cross-section of real pipeline vol.

Runs a single tile through the pipeline (via _pipeline_runner.run_tile_prelude),
builds the full voxel volume via build_column_array (with geology if enabled),
then slices along a sample line and renders the block identities as a PNG.

This is the PRIMARY validation tool for geology work — no Minecraft needed.

CLI:
    py diag_cliff_crosssection.py --tile-x 24 --tile-z 80 \
        --line 50 50 460 460 \
        --y-min 50 --y-max 300 \
        --out diag_output/24_80/cliff_crosssection.png

    Defaults: tile (24,80), diagonal line, Y range auto-cropped to terrain.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

# Force UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Block color palette — maps block names to RGB for the cross-section image
# ---------------------------------------------------------------------------

BLOCK_COLORS: dict[str, tuple[int, int, int]] = {
    # Air / water
    "air":               (200, 220, 240),
    "water":             (40, 80, 200),
    # Surface organic
    "grass_block":       (80, 170, 60),
    "snow_block":        (240, 245, 255),
    "powder_snow":       (230, 235, 245),
    "sand":              (220, 200, 130),
    "red_sand":          (180, 100, 50),
    "gravel":            (140, 135, 130),
    "coarse_dirt":       (120, 85, 55),
    "dirt":              (135, 95, 60),
    "packed_mud":        (145, 110, 75),
    "podzol":            (100, 75, 40),
    "mud":               (80, 65, 50),
    "clay":              (150, 155, 165),
    # Stone / cliff
    "stone":             (128, 128, 128),
    "andesite":          (110, 110, 110),
    "diorite":           (180, 180, 180),
    "granite":           (160, 110, 90),
    "polished_granite":  (155, 105, 85),
    "cobblestone":       (115, 115, 115),
    "mossy_cobblestone": (100, 120, 90),
    "tuff":              (95, 100, 95),
    "calcite":           (200, 200, 195),
    "smooth_stone":      (155, 155, 155),
    # Deepslate / basement
    "deepslate":         (60, 60, 68),
    "cobbled_deepslate": (65, 65, 73),
    "bedrock":           (30, 30, 30),
    # Basalt
    "basalt":            (50, 50, 55),
    "smooth_basalt":     (55, 55, 60),
    "blackstone":        (35, 35, 40),
    # Sandstone
    "sandstone":         (200, 185, 130),
    "smooth_sandstone":  (210, 195, 140),
    "red_sandstone":     (170, 90, 45),
    # Terracotta
    "terracotta":        (160, 100, 65),
    "orange_terracotta": (165, 85, 35),
    "white_terracotta":  (210, 180, 165),
    "brown_terracotta":  (80, 55, 35),
    "red_terracotta":    (145, 65, 40),
    "yellow_terracotta": (190, 140, 40),
    # Ice
    "ice":               (160, 195, 240),
    "packed_ice":        (140, 175, 225),
    "blue_ice":          (110, 150, 210),
}

_FALLBACK_COLOR = (255, 0, 255)  # magenta = unmapped block


def block_color(name: str) -> tuple[int, int, int]:
    """Return RGB for a block name, stripping any [state] suffix."""
    base = name.split("[")[0] if "[" in name else name
    return BLOCK_COLORS.get(base, BLOCK_COLORS.get(name, _FALLBACK_COLOR))


# ---------------------------------------------------------------------------
# Cross-section renderer
# ---------------------------------------------------------------------------

def sample_line(
    vol: np.ndarray,
    pal,
    surface_y: np.ndarray,
    x0: int, z0: int, x1: int, z1: int,
    y_min: int | None = None,
    y_max: int | None = None,
) -> tuple[np.ndarray, int, int, np.ndarray, np.ndarray, np.ndarray, set]:
    """
    Sample a vertical cross-section along a line from (x0,z0) to (x1,z1).

    Returns (img, y_min, y_max, xs, zs, sy_along, blocks_seen).
    img: RGB image (height, length, 3) with high Y at the top.
    Coordinates are tile-local (0-511): x=column, z=row.
    """
    Y_MIN = -64  # chunk_writer.Y_MIN
    Y_RANGE = vol.shape[0]

    length = max(abs(x1 - x0), abs(z1 - z0)) + 1
    xs = np.clip(np.linspace(x0, x1, length).astype(int), 0, vol.shape[2] - 1)
    zs = np.clip(np.linspace(z0, z1, length).astype(int), 0, vol.shape[1] - 1)

    # Auto y range from surface profile
    sy_along = np.array([surface_y[zs[i], xs[i]] for i in range(length)])
    if y_min is None:
        y_min = max(int(sy_along.min()) - 40, Y_MIN)
    if y_max is None:
        y_max = min(int(sy_along.max()) + 20, Y_MIN + Y_RANGE - 1)

    height = y_max - y_min + 1
    img = np.zeros((height, length, 3), dtype=np.uint8)
    blocks_seen: set[str] = set()

    for col_i in range(length):
        cx, cz = int(xs[col_i]), int(zs[col_i])
        for y_mc in range(y_min, y_max + 1):
            yi = y_mc - Y_MIN
            if 0 <= yi < Y_RANGE:
                block_name = pal.name_of(vol[yi, cz, cx])
            else:
                block_name = "air"
            blocks_seen.add(block_name)
            row = y_max - y_mc  # high Y at top
            img[row, col_i] = block_color(block_name)

    return img, y_min, y_max, xs, zs, sy_along, blocks_seen


def render_annotated(
    img: np.ndarray,
    y_min: int, y_max: int,
    sy_along: np.ndarray,
    tile_x: int, tile_z: int,
    line: tuple[int, int, int, int],
    blocks_seen: set[str],
    geology_enabled: bool,
) -> np.ndarray:
    """Add Y-axis labels, surface line, title, and legend."""
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 10)
    except (OSError, IOError):
        try:
            # Windows fallback
            font = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 10)
        except (OSError, IOError):
            font = ImageFont.load_default()

    MARGIN_L = 50
    h, w = img.shape[:2]

    # Legend
    legend_blocks = sorted(blocks_seen - {"air"})
    SWATCH = 12
    PAD = 3
    legend_h = max(h, len(legend_blocks) * (SWATCH + PAD) + PAD * 4)
    LEGEND_W = 200

    total_w = MARGIN_L + w + 10 + LEGEND_W
    total_h = max(h, legend_h) + 20  # 20 for title
    canvas = np.full((total_h, total_w, 3), 255, dtype=np.uint8)

    # Paste cross-section
    canvas[20:20+h, MARGIN_L:MARGIN_L+w] = img

    pil_img = Image.fromarray(canvas, mode="RGB")
    draw = ImageDraw.Draw(pil_img)

    # Title
    geo_str = "GEOLOGY ON" if geology_enabled else "geology off"
    title = f"tile({tile_x},{tile_z})  line({line[0]},{line[1]})->({line[2]},{line[3]})  Y[{y_min},{y_max}]  [{geo_str}]"
    draw.text((MARGIN_L, 3), title, fill=(0, 0, 0), font=font)

    # Y-axis labels every 20 blocks
    for y_mc in range(y_min, y_max + 1, 20):
        row = 20 + (y_max - y_mc)
        if 0 <= row < total_h:
            draw.text((2, row - 5), f"Y{y_mc}", fill=(80, 80, 80), font=font)
            # Grid line
            for x in range(MARGIN_L, MARGIN_L + w, 4):
                if 0 <= row < total_h:
                    canvas = np.array(pil_img)
                    canvas[row, x] = (200, 200, 200)
                    pil_img = Image.fromarray(canvas)
                    draw = ImageDraw.Draw(pil_img)

    # Surface profile (orange line)
    canvas = np.array(pil_img)
    for col_i in range(len(sy_along)):
        sy = int(sy_along[col_i])
        row = 20 + (y_max - sy)
        x = MARGIN_L + col_i
        for dr in range(-1, 2):
            for dx in range(-1, 2):
                r, c = row + dr, x + dx
                if 0 <= r < total_h and 0 <= c < total_w:
                    canvas[r, c] = (255, 120, 0)

    # Legend
    legend_x = MARGIN_L + w + 10
    legend_y = 20
    for i, bname in enumerate(legend_blocks):
        y = legend_y + i * (SWATCH + PAD)
        color = block_color(bname)
        canvas[y:y+SWATCH, legend_x:legend_x+SWATCH] = color
        # Border
        canvas[y, legend_x:legend_x+SWATCH] = (0, 0, 0)
        canvas[y+SWATCH-1, legend_x:legend_x+SWATCH] = (0, 0, 0)
        canvas[y:y+SWATCH, legend_x] = (0, 0, 0)
        canvas[y:y+SWATCH, legend_x+SWATCH-1] = (0, 0, 0)

    pil_img = Image.fromarray(canvas)
    draw = ImageDraw.Draw(pil_img)
    for i, bname in enumerate(legend_blocks):
        y = legend_y + i * (SWATCH + PAD)
        draw.text((legend_x + SWATCH + 4, y), bname, fill=(0, 0, 0), font=font)

    return np.array(pil_img)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tile-x", type=int, default=24)
    p.add_argument("--tile-z", type=int, default=80)
    p.add_argument("--line", type=int, nargs=4, metavar=("X0", "Z0", "X1", "Z1"),
                   default=None,
                   help="Sample line endpoints in tile-local coords (0-511). Default: diagonal.")
    p.add_argument("--y-min", type=int, default=None)
    p.add_argument("--y-max", type=int, default=None)
    p.add_argument("--config", type=str, default="config/thresholds.json")
    p.add_argument("--masks", type=str, default="masks/")
    p.add_argument("--out", type=str,
                   default="diag_output/{tx}_{tz}/cliff_crosssection.png")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    tx, tz = args.tile_x, args.tile_z
    config_path = Path(args.config)
    masks_dir = Path(args.masks)
    out_path = Path(args.out.format(tx=tx, tz=tz))
    line = tuple(args.line) if args.line else (50, 50, 460, 460)

    cfg = json.loads(config_path.read_text())

    # ---- Run pipeline through surface decoration ----
    t0 = time.time()
    print(f"[xsec] Running pipeline for tile ({tx},{tz})...")

    from _pipeline_runner import run_tile_prelude
    art = run_tile_prelude(
        tile_x=tx, tile_z=tz,
        cfg=cfg, masks_dir=masks_dir, cfg_path=config_path,
        verbose=True,
    )
    print(f"[xsec] Pipeline prelude: {art.elapsed_ms}ms")

    # ---- Build voxel volume ----
    from core import chunk_writer as cw

    _use_geo = bool(cfg.get("lithology", {}).get("feature_flag_enabled", False))
    _cb = cfg.get("cliff_banding", {})
    tile_world_x = tx * 512
    tile_world_z = tz * 512

    print("[xsec] Building volume array...")
    vol, pal = cw.build_column_array(
        art.surface_y, art.surface_blk, art.sub_blk, art.ground_cover,
        biome_grid=art.biome_grid,
        cliff_deg_thr=float(_cb.get("cliff_deg_thr", 45.0)),
        band_scale_y=int(_cb.get("band_scale_y", 12)),
        tile_world_x=tile_world_x,
        tile_world_z=tile_world_z,
        lithology_tile=art.lithology_tile if _use_geo else None,
        use_new_geology=_use_geo,
        flow_tile=art.masks["flow"] if _use_geo else None,
        cfg=cfg if _use_geo else None,
    )
    t_vol = time.time() - t0
    print(f"[xsec] Volume built in {t_vol:.1f}s total, palette: {len(pal._names)} blocks")

    # ---- Sample cross-section ----
    print(f"[xsec] Sampling line ({line[0]},{line[1]}) -> ({line[2]},{line[3]})...")
    img, y_min, y_max, xs, zs, sy_along, blocks_seen = sample_line(
        vol, pal, art.surface_y,
        line[0], line[1], line[2], line[3],
        y_min=args.y_min, y_max=args.y_max,
    )

    # ---- Render annotated image ----
    annotated = render_annotated(
        img, y_min, y_max, sy_along, tx, tz, line, blocks_seen, _use_geo,
    )

    # ---- Save ----
    out_path.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image as PILImage
    PILImage.fromarray(annotated).save(out_path)

    meta = {
        "tile_x": tx, "tile_z": tz,
        "line": list(line),
        "y_range": [y_min, y_max],
        "image_size": [annotated.shape[1], annotated.shape[0]],
        "blocks_seen": sorted(blocks_seen - {"air"}),
        "pipeline_ms": art.elapsed_ms,
        "total_seconds": round(time.time() - t0, 1),
        "geology_enabled": _use_geo,
        "phase": "1.75b",
        "stub": False,
    }
    out_path.with_suffix(".json").write_text(json.dumps(meta, indent=2))

    print(f"[xsec] Wrote {out_path} ({annotated.shape[1]}x{annotated.shape[0]})")
    print(f"[xsec] Blocks seen: {sorted(blocks_seen - {'air'})}")
    print(f"[xsec] Total time: {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
