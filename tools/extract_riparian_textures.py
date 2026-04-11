"""extract_riparian_textures.py — Phase 0.5 (S44).

Spec: PHYSICAL_REALISM_REFACTOR.md §6 Pass 2 "Riparian fringe palette" +
§11 Phase 0.5 texture sanity artifact.

Pulls the block texture PNGs that compose the temperate riparian palette
out of the vanilla client jar, drops them into `diag_output/riparian/` as
individual 16×16 PNGs, AND composes a single sanity swatch image for
visual review (`diag_output/riparian/_palette_sanity.png`).

The riparian palette is the Phase 2 target for the Pass 2 temperate
riparian fringe layer. Having the raw textures on disk lets us tune the
in-game placement by eye against the real block appearance without
spinning up a server.

Blocks extracted (1.21.10 vanilla assets):
    dirt              assets/minecraft/textures/block/dirt.png
    coarse_dirt       assets/minecraft/textures/block/coarse_dirt.png
    rooted_dirt       assets/minecraft/textures/block/rooted_dirt.png
    podzol            assets/minecraft/textures/block/podzol_top.png
    mud               assets/minecraft/textures/block/mud.png
    gravel            assets/minecraft/textures/block/gravel.png
    clay              assets/minecraft/textures/block/clay.png
    packed_mud        assets/minecraft/textures/block/packed_mud.png

Usage:
    py tools/extract_riparian_textures.py
    py tools/extract_riparian_textures.py --jar /path/to/1.21.10.jar
"""
from __future__ import annotations

import argparse
import io
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_JAR = Path(
    "/sessions/practical-happy-bardeen/mnt/ModrinthApp/meta/versions/1.21.10/1.21.10.jar"
)

RIPARIAN_TEXTURES = {
    "dirt":        "assets/minecraft/textures/block/dirt.png",
    "coarse_dirt": "assets/minecraft/textures/block/coarse_dirt.png",
    "rooted_dirt": "assets/minecraft/textures/block/rooted_dirt.png",
    "podzol":      "assets/minecraft/textures/block/podzol_top.png",
    "mud":         "assets/minecraft/textures/block/mud.png",
    "gravel":      "assets/minecraft/textures/block/gravel.png",
    "clay":        "assets/minecraft/textures/block/clay.png",
    "packed_mud":  "assets/minecraft/textures/block/packed_mud.png",
}


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--jar", type=Path, default=DEFAULT_JAR)
    p.add_argument("--out", type=Path,
                   default=REPO_ROOT / "diag_output" / "riparian")
    return p.parse_args()


def _compose_sanity_swatch(tex_bytes: dict[str, bytes], out_path: Path) -> None:
    """Tile each block 4× horizontally with a label band, then stack vertically."""
    from PIL import Image, ImageDraw, ImageFont

    tile_w = 64   # 16×4
    tile_h = 64
    label_h = 16
    row_h = tile_h + label_h
    rows = len(tex_bytes)
    canvas = Image.new("RGB", (tile_w, rows * row_h), (30, 30, 30))
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None
    draw = ImageDraw.Draw(canvas)
    for i, (name, data) in enumerate(tex_bytes.items()):
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img = img.resize((16, 16), Image.NEAREST)
        tile = Image.new("RGB", (tile_w, tile_h))
        for tx in range(0, tile_w, 16):
            for ty in range(0, tile_h, 16):
                tile.paste(img, (tx, ty))
        y0 = i * row_h
        canvas.paste(tile, (0, y0))
        draw.text((4, y0 + tile_h + 2), name, fill=(220, 220, 220), font=font)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)


def main() -> int:
    args = _parse_args()
    if not args.jar.exists():
        print(f"[extract_riparian_textures] FATAL: jar not found: {args.jar}",
              file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)

    tex_bytes: dict[str, bytes] = {}
    with zipfile.ZipFile(args.jar) as zf:
        for name, jar_path in RIPARIAN_TEXTURES.items():
            try:
                data = zf.read(jar_path)
            except KeyError:
                print(f"[extract_riparian_textures] WARN: {jar_path} missing from jar")
                continue
            dst = args.out / f"{name}.png"
            dst.write_bytes(data)
            tex_bytes[name] = data
            print(f"[extract_riparian_textures] wrote {dst} ({len(data)} bytes)")

    if not tex_bytes:
        print("[extract_riparian_textures] FATAL: no textures extracted",
              file=sys.stderr)
        return 2

    sanity_path = args.out / "_palette_sanity.png"
    try:
        _compose_sanity_swatch(tex_bytes, sanity_path)
        print(f"[extract_riparian_textures] wrote sanity swatch {sanity_path}")
    except ImportError:
        print("[extract_riparian_textures] Pillow missing — skipping sanity swatch")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
