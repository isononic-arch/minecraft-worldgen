"""Per-biome best reference tile sampler.

For each biome in OVERRIDE_BIOME_MAP, scan the 97x97 tile grid and pick the
tile with the highest pixel count of that biome. Emits:
- memory/biome_reference_tiles.csv  (biome, tile_x, tile_z, pct, tp)
- memory/BIOME_VALIDATOR_CHECKLIST.md  (checkable markdown table)

Usage:
    py tools/diag_biome_sampler.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import rasterio

_WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_WORKTREE))
from core.biome_assignment import OVERRIDE_BIOME_MAP  # noqa: E402

MASK_PATH = Path(r"C:/Users/nicho/minecraft-worldgen/masks/override.tif")
CSV_PATH = _WORKTREE / "memory" / "biome_reference_tiles.csv"
CHECKLIST_PATH = _WORKTREE / "memory" / "BIOME_VALIDATOR_CHECKLIST.md"

TILE = 512
GRID = 97

TP_Y_BY_BIOME = {
    "BOREAL_ALPINE": 250,
    "SNOWY_BOREAL_TAIGA": 220,
    "ARCTIC_TUNDRA": 180,
    "FROZEN_FLATS": 160,
    "BOREAL_TAIGA": 200,
    "MIXED_FOREST": 200,
    "TEMPERATE_DECIDUOUS": 180,
    "BIRCH_FOREST": 180,
    "TEMPERATE_RAINFOREST": 180,
    "COASTAL_HEATH": 90,
    "RAINFOREST_COAST": 90,
    "EASTERN_TEMPERATE_COAST": 90,
    "LUSH_RAINFOREST_COAST": 90,
    "MANGROVE_COAST": 80,
    "TIDAL_JUNGLE_FRINGE": 90,
    "FRESHWATER_FEN": 80,
    "SAND_DUNE_DESERT": 120,
    "DESERT_STEPPE_TRANSITION": 120,
    "SEMI_ARID_SHRUBLAND": 140,
    "DRY_OAK_SAVANNA": 130,
    "DRY_PINE_BARRENS": 140,
    "CONTINENTAL_STEPPE": 140,
    "SCRUBBY_HEATHLAND": 140,
    "KARST_BARRENS": 160,
    "DRY_WOODLAND_MAQUIS": 140,
    "RIPARIAN_WOODLAND": 100,
}


def main() -> int:
    print(f"[biome_sampler] reading {MASK_PATH}", flush=True)
    with rasterio.open(MASK_PATH) as src:
        override = src.read(1)
    print(f"[biome_sampler] override shape={override.shape}", flush=True)

    biomes = [(z, n) for z, n in OVERRIDE_BIOME_MAP.items() if n]
    print(f"[biome_sampler] scanning {len(biomes)} biomes on {GRID}x{GRID} grid", flush=True)

    rows = []
    for zone, name in biomes:
        mask = (override == zone)
        if not mask.any():
            rows.append({
                "biome": name, "zone": zone, "tile_x": None, "tile_z": None,
                "px_count": 0, "pct": 0.0, "tp": "(biome not present)",
            })
            print(f"  {name:26s} zone={zone:3d} NOT PRESENT", flush=True)
            continue

        best_count = 0
        best_tx, best_tz = -1, -1
        for tz in range(GRID):
            y0 = tz * TILE
            y1 = y0 + TILE
            for tx in range(GRID):
                x0 = tx * TILE
                x1 = x0 + TILE
                cnt = int(mask[y0:y1, x0:x1].sum())
                if cnt > best_count:
                    best_count = cnt
                    best_tx, best_tz = tx, tz

        pct = 100.0 * best_count / (TILE * TILE)
        # S86: TP to the BIOME CENTROID within the best tile, not the tile
        # center. Tiles where the biome covers only part of the area (e.g.
        # MANGROVE_COAST at 55% with ocean on the other side) used to TP into
        # ocean. Centroid lands you reliably in the actual biome pixels.
        y0 = best_tz * TILE
        x0 = best_tx * TILE
        tile_mask = mask[y0:y0 + TILE, x0:x0 + TILE]
        ys, xs = np.where(tile_mask)
        if len(ys):
            # Median is more robust than mean against fringe pixels
            local_y = int(np.median(ys))
            local_x = int(np.median(xs))
            world_x = x0 + local_x
            world_z = y0 + local_y
        else:
            # Shouldn't happen since best_count > 0, but fall back to center
            world_x = best_tx * TILE + TILE // 2
            world_z = best_tz * TILE + TILE // 2
        y_tp = TP_Y_BY_BIOME.get(name, 200)
        tp = f"/tp @s {world_x} {y_tp} {world_z}"
        rows.append({
            "biome": name, "zone": zone, "tile_x": best_tx, "tile_z": best_tz,
            "px_count": best_count, "pct": pct, "tp": tp,
        })
        print(f"  {name:26s} zone={zone:3d} tile=({best_tx:2d},{best_tz:2d}) pct={pct:5.1f}%  tp={tp}", flush=True)

    CSV_PATH.parent.mkdir(exist_ok=True)
    csv_lines = ["biome,zone,tile_x,tile_z,px_count,pct,tp_command"]
    for r in rows:
        tx = r["tile_x"] if r["tile_x"] is not None else ""
        tz = r["tile_z"] if r["tile_z"] is not None else ""
        csv_lines.append(f"{r['biome']},{r['zone']},{tx},{tz},{r['px_count']},{r['pct']:.2f},{r['tp']}")
    CSV_PATH.write_text("\n".join(csv_lines), encoding="utf-8")
    print(f"[biome_sampler] CSV written to {CSV_PATH}", flush=True)

    md_lines = [
        "# Biome Validator Checklist",
        "",
        "Auto-generated. For each biome, `best tile` = tile with most pixels of that biome in `override.tif`.",
        "",
        "Instructions: walk each biome in-world via the TP command. Mark columns Y/N.",
        "",
        "| Biome | Zone | Best tile | Pure % | TP | Visually OK | GC OK | Schematics OK | Palette OK | Notes |",
        "|-------|------|-----------|--------|----|-------------|-------|---------------|------------|-------|",
    ]
    for r in rows:
        tile_str = f"({r['tile_x']},{r['tile_z']})" if r["tile_x"] is not None else "(absent)"
        md_lines.append(
            f"| {r['biome']} | {r['zone']} | {tile_str} | {r['pct']:.1f}% | `{r['tp']}` | [ ] | [ ] | [ ] | [ ] |  |"
        )

    md_lines.extend([
        "",
        "## Legend",
        "- **Visually OK**: overall in-world first impression matches the intent.",
        "- **GC OK**: ground cover density + species mix looks right for the biome.",
        "- **Schematics OK**: trees/bushes placed at correct density + sitting on ground.",
        "- **Palette OK**: surface + subsurface blocks read as the right geology.",
        "- **Notes**: anything worth a follow-up NICK PRIORITIES entry.",
    ])
    CHECKLIST_PATH.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"[biome_sampler] checklist written to {CHECKLIST_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
