"""S86/S87 3G: Render tree-schematic cross-sections for a biome key.

Usage:
    py tools/diag_tree_cross_section.py [BIOME_KEY]

Default biome key: 'dpine' (DRY_PINE_BARRENS).  Use any key from
schematic_index.json (e.g. 'maquis', 'btaiga', 'mixed', 'dosav').

Outputs: tree_cross_section_<key>.png

For each tree schematic in the chosen biome, loads the .schem file, projects
along the +Z axis (depth into page), and renders the resulting side-on
silhouette colored by block name.  Trees laid out in a row with name + size
labels so the user can pick which ones to drop from the biome's routing.
"""
from __future__ import annotations
import sys
import json
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt

ROOT = Path(r"C:\Users\nicho\minecraft-worldgen")
sys.path.insert(0, str(ROOT))
from core.schematic_loader import load_schem  # noqa: E402
INDEX_PATH = ROOT / "schematic_index.json"
VEGETATION_DIR = ROOT / "Vegetation"

# Colors for common block families - just enough to distinguish trunk vs leaves
COLORS = {
    "log": "#4a3019",
    "wood": "#4a3019",
    "stem": "#5a4029",
    "leaves": "#3d8b3d",
    "leaf": "#3d8b3d",
    "azalea_leaves": "#4d9b4d",
    "moss_block": "#386a36",
    "moss_carpet": "#386a36",
    "vine": "#496f30",
    "bush": "#6b5b3a",
    "sapling": "#6a8a3a",
    "fungus": "#a04030",
    "wart_block": "#852f2f",
    "shroomlight": "#e8a040",
    "berries": "#c63030",
    "cocoa": "#7a4a26",
    "snow": "#eef4f8",
    "ice": "#bcd4e6",
    "air": None,
    "void_air": None,
    "cave_air": None,
}


def block_color(name: str) -> str | None:
    """Map MC block name to a side-profile color, or None for air-like."""
    n = name.lower().split(":", 1)[-1]
    n = n.split("[", 1)[0]  # strip block-state suffix (avoids "log" matching "waterlogged")
    if n in COLORS:
        return COLORS[n]
    for kw, c in COLORS.items():
        if kw in n:
            return c
    return "#888888"  # unknown -> gray (shouldn't see this on trees)


def side_profile(blocks: np.ndarray) -> np.ndarray:
    """Project (Y, Z, X) block-name array to side-view (Y, X) RGB.
    For each (x, y) column, pick the NEAREST non-air block along Z (from +Z back)."""
    H, L, W = blocks.shape
    rgb = np.full((H, W, 3), 60, dtype=np.uint8)  # dark bg
    for x in range(W):
        for y in range(H):
            for z in range(L - 1, -1, -1):
                name = blocks[y, z, x]
                if not name:
                    continue
                c = block_color(str(name))
                if c is None:
                    continue
                r, g, b = int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)
                rgb[H - 1 - y, x] = (r, g, b)
                break
    return rgb


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else "dpine"
    print(f"Loading schematic_index.json...")
    idx = json.loads(INDEX_PATH.read_text())
    if key not in idx:
        print(f"ERROR: key {key!r} not in index. Available: {sorted(idx.keys())}")
        return 1
    items = idx[key]
    match = sys.argv[2] if len(sys.argv) > 2 else "_tree_"   # 2nd arg: path substring (e.g. "bush_" for foliage)
    trees = [it for it in items if match in it.get("path", "")]
    _seen = set(); trees = [t for t in trees if not (t["path"] in _seen or _seen.add(t["path"]))]
    if not trees:
        print(f"No schematics matching {match!r} for biome key {key!r}.")
        return 1
    print(f"Biome key: {key}, {len(trees)} tree schematics")

    profiles = []
    for it in trees:
        p = ROOT / it["path"]
        if not p.exists():
            print(f"  MISSING: {p}")
            continue
        try:
            sd = load_schem(p)
            rgb = side_profile(sd.blocks)
            stem = p.stem
            profiles.append((stem, rgb, sd.height, sd.width))
            print(f"  {stem}: {sd.width}x{sd.height}x{sd.length}")
        except Exception as e:
            print(f"  ERROR loading {p.name}: {e}")

    if not profiles:
        print("No profiles rendered.")
        return 1

    # Stack horizontally with name labels.  Max H + 50px label header.
    max_h = max(p[2] for p in profiles)
    panel_w = max(p[3] for p in profiles)
    n = len(profiles)
    n_cols = min(n, 5)
    n_rows = (n + n_cols - 1) // n_cols
    fig_w = max(14, panel_w * n_cols * 0.15)
    fig_h = max(8, max_h * n_rows * 0.18)
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(fig_w, fig_h),
                              facecolor="#202028")
    axes = np.atleast_2d(axes)
    for i, (stem, rgb, h, w) in enumerate(profiles):
        ax = axes[i // n_cols, i % n_cols]
        # Pad to max_h x panel_w for visual alignment
        padded = np.full((max_h, panel_w, 3), 60, dtype=np.uint8)
        padded[max_h - h:, :w] = rgb
        ax.imshow(padded)
        # Pretty title
        title = stem.replace(f"{key}_tree_", "")
        ax.set_title(f"{title}\n({w}x{h})", fontsize=9, color="white")
        ax.set_facecolor("#202028")
        ax.set_xticks([])
        ax.set_yticks([])
    # Hide unused axes
    for j in range(n, n_rows * n_cols):
        axes[j // n_cols, j % n_cols].axis("off")
    plt.suptitle(f"Tree cross-sections — biome key '{key}' ({n} species)",
                 color="white", fontsize=13)
    plt.tight_layout()
    out = ROOT / f"tree_cross_section_{key}.png"
    plt.savefig(out, dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
    print(f"Saved {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
