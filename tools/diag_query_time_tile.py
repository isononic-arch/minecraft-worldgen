"""Topdown preview of the query-time gap sampler at a single tile.

Samples rock_gap + snow_gap via Catmull-Rom at tile coordinates (no dither by
default), renders side-by-side with the baked versions for eyeballing.
Runs in ~5s — no .mca, just a PNG diagnostic.

Usage:
    py tools/diag_query_time_tile.py --tile-x 25 --tile-z 80
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window

_WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_WORKTREE))
from core.gaea_gap_sampler import sample_gap_at_tile, build_gap_config  # noqa: E402

MASKS = Path(r"C:/Users/nicho/minecraft-worldgen/masks")
CONFIG = _WORKTREE / "config" / "thresholds.json"


def read_tile_baked(path: Path, tx: int, tz: int) -> np.ndarray:
    with rasterio.open(path) as src:
        return src.read(1, window=Window(tx * 512, tz * 512, 512, 512)).astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tile-x", type=int, default=25)
    ap.add_argument("--tile-z", type=int, default=80)
    ap.add_argument("--out", default=None, help="Output PNG path (default memory/query_time_<tx>_<tz>.png)")
    args = ap.parse_args()

    tx, tz = args.tile_x, args.tile_z
    out = Path(args.out) if args.out else (_WORKTREE / "memory" / f"query_time_{tx}_{tz}.png")

    cfg = json.load(open(CONFIG))
    gap_cfg = build_gap_config(cfg.get("gaea_gaps", {}), MASKS)
    if not gap_cfg:
        print("gap_cfg empty — check config.gaea_gaps.use_query_time + 8k source files in masks/")
        return 1

    print(f"[query_time_tile] tile ({tx},{tz}) — sampling query-time + reading baked", flush=True)

    # Query-time with current config (dither=none by default)
    rock_qt = sample_gap_at_tile(col_off=tx*512, row_off=tz*512, width=512, height=512,
                                 **{k: v for k, v in gap_cfg["rock_gap"].items()})
    snow_qt = sample_gap_at_tile(col_off=tx*512, row_off=tz*512, width=512, height=512,
                                 **{k: v for k, v in gap_cfg["snow_gap"].items()})

    # Baked cubic_spline + blue_noise (original S56 path)
    rock_baked = read_tile_baked(MASKS / "rock_gap.tif", tx, tz)
    snow_baked = read_tile_baked(MASKS / "snow_gap.tif", tx, tz)

    # Baked catmull_rom + blue_noise (S60 experimental)
    rock_catm_baked = read_tile_baked(MASKS / "rock_gap_catmull.tif", tx, tz) if (MASKS / "rock_gap_catmull.tif").exists() else None
    snow_catm_baked = read_tile_baked(MASKS / "snow_gap_catmull.tif", tx, tz) if (MASKS / "snow_gap_catmull.tif").exists() else None

    print(f"  rock  query-time: mean={rock_qt.mean():.3f}   baked(cubic)={rock_baked.mean():.3f}"
          f"{'   baked(catmull)='+f'{rock_catm_baked.mean():.3f}' if rock_catm_baked is not None else ''}")
    print(f"  snow  query-time: mean={snow_qt.mean():.3f}   baked(cubic)={snow_baked.mean():.3f}"
          f"{'   baked(catmull)='+f'{snow_catm_baked.mean():.3f}' if snow_catm_baked is not None else ''}")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n_col = 3 if rock_catm_baked is not None else 2
    fig, axes = plt.subplots(2, n_col, figsize=(n_col * 5, 10))

    row_titles_rock = ["rock_gap query-time\n(catmull_rom, dither=none — SHARP)",
                       "rock_gap baked cubic+blue_noise\n(current default)"]
    if rock_catm_baked is not None:
        row_titles_rock.append("rock_gap baked catmull+blue_noise\n(S60 A/B)")
    row_titles_snow = ["snow_gap query-time\n(catmull_rom, dither=none — SHARP)",
                       "snow_gap baked cubic+blue_noise"]
    if snow_catm_baked is not None:
        row_titles_snow.append("snow_gap baked catmull+blue_noise")

    imgs_rock = [rock_qt, rock_baked]
    imgs_snow = [snow_qt, snow_baked]
    if rock_catm_baked is not None:
        imgs_rock.append(rock_catm_baked); imgs_snow.append(snow_catm_baked)

    for col, (img, title) in enumerate(zip(imgs_rock, row_titles_rock)):
        axes[0, col].imshow(img, cmap="gray_r", vmin=0, vmax=1, interpolation="nearest")
        axes[0, col].set_title(title, fontsize=10)
        axes[0, col].set_xticks([]); axes[0, col].set_yticks([])
    for col, (img, title) in enumerate(zip(imgs_snow, row_titles_snow)):
        axes[1, col].imshow(img, cmap="Blues", vmin=0, vmax=1, interpolation="nearest")
        axes[1, col].set_title(title, fontsize=10)
        axes[1, col].set_xticks([]); axes[1, col].set_yticks([])

    fig.suptitle(f"Query-time vs baked masks — tile ({tx},{tz})  512×512 px", fontsize=13)
    fig.tight_layout()
    out.parent.mkdir(exist_ok=True)
    fig.savefig(out, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print(f"[query_time_tile] wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
