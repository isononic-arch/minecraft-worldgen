"""
diag_v12_vs_v11_compare.py - 4-tile comparison PNG, v11 vs v12.

Top row: v11  (smoothed trough, no geomorph)
Bottom row: v12 (+ thalweg asymmetry + bedform + riffle-pool)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diag_mca_topdown import render_region


def label_row(img: np.ndarray, label: str) -> np.ndarray:
    pil = Image.fromarray(img)
    drw = ImageDraw.Draw(pil)
    try:
        font = ImageFont.truetype("arial.ttf", 24)
    except Exception:
        font = ImageFont.load_default()
    drw.rectangle([(4, 4), (320, 36)], fill=(0, 0, 0))
    drw.text((8, 6), label, fill=(255, 255, 255), font=font)
    return np.asarray(pil)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--v11-dir", required=True)
    p.add_argument("--v12-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--tx0", type=int, default=51)
    p.add_argument("--tx1", type=int, default=52)
    p.add_argument("--tz", type=int, default=53)
    args = p.parse_args()

    dirs = {"v11": Path(args.v11_dir), "v12": Path(args.v12_dir)}
    rendered = {}
    for label, d in dirs.items():
        for tx in (args.tx0, args.tx1):
            path = d / f"r.{tx}.{args.tz}.mca"
            if not path.is_file():
                print(f"ERROR: {path} not found", file=sys.stderr)
                return 1
            print(f"Rendering {label} {path.name}...", file=sys.stderr)
            rendered[(label, tx)] = render_region(str(path))

    H, W = 512, 1024
    out = np.zeros((H * 2, W, 3), dtype=np.uint8)
    out[:H, :512] = rendered[("v11", args.tx0)]
    out[:H, 512:] = rendered[("v11", args.tx1)]
    out[H:, :512] = rendered[("v12", args.tx0)]
    out[H:, 512:] = rendered[("v12", args.tx1)]

    out[:H] = label_row(out[:H], "v11 (smoothed trough)")
    out[H:] = label_row(out[H:], "v12 (+ thalweg + bedform + riffle)")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(out).save(str(args.out))
    print(f"Saved {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
