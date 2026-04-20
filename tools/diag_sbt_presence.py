"""One-shot SBT presence scan.

Reads masks/override.tif, finds all zone-35 (SNOWY_BOREAL_TAIGA) pixels,
runs connected-components labeling, and for each region > 100 px emits:
area, bounding box in tile coords, top-3 adjoining biomes by shared-boundary
pixel count.

Output: memory/sbt_presence_report.md

Usage:
    py tools/diag_sbt_presence.py

Runtime ~30s on 50k mask.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import rasterio
from scipy import ndimage as ndi

_WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_WORKTREE))
from core.biome_assignment import OVERRIDE_BIOME_MAP  # noqa: E402

SBT_ZONE = 35
TILE_SIZE = 512
MIN_REGION_PX = 100
MASK_PATH = Path(r"C:/Users/nicho/minecraft-worldgen/masks/override.tif")
OUT_PATH = _WORKTREE / "memory" / "sbt_presence_report.md"


def tile_coord(px: int) -> int:
    return px // TILE_SIZE


def main() -> int:
    print(f"[sbt_scan] reading {MASK_PATH} ...", flush=True)
    with rasterio.open(MASK_PATH) as src:
        override = src.read(1)
    print(f"[sbt_scan] override shape={override.shape} dtype={override.dtype}", flush=True)

    sbt_mask = (override == SBT_ZONE)
    sbt_total = int(sbt_mask.sum())
    print(f"[sbt_scan] total zone-35 pixels: {sbt_total:,}", flush=True)
    if sbt_total == 0:
        OUT_PATH.write_text("# SBT Presence Report\n\nNo zone-35 (SNOWY_BOREAL_TAIGA) pixels found in override.tif.\n")
        print("[sbt_scan] no SBT pixels; report written with empty finding.")
        return 0

    print("[sbt_scan] downsampling to 1:8 (6250x6250) for connected-components ...", flush=True)
    # Connected components at 50k would allocate ~9-18 GiB; downsample to 1:8.
    # Any 8x8 source block with ANY SBT pixel is treated as SBT at reduced res.
    # Each reduced pixel represents 64 world-blocks (8x8 blocks area).
    SCALE = 8
    H, W = sbt_mask.shape
    h8, w8 = H // SCALE, W // SCALE
    sbt_8 = sbt_mask[:h8*SCALE, :w8*SCALE].reshape(h8, SCALE, w8, SCALE).any(axis=(1,3))
    override_8 = override[:h8*SCALE:SCALE, :w8*SCALE:SCALE]
    print(f"[sbt_scan] reduced shape={sbt_8.shape}, reduced SBT pixels: {int(sbt_8.sum()):,}", flush=True)

    print("[sbt_scan] labeling connected components at 1:8 ...", flush=True)
    labeled, n_regions = ndi.label(sbt_8)
    print(f"[sbt_scan] total SBT regions (1:8): {n_regions:,}", flush=True)

    sizes = np.bincount(labeled.ravel())
    region_ids = np.argsort(sizes[1:])[::-1] + 1
    MIN_REDUCED = max(1, MIN_REGION_PX // (SCALE*SCALE))
    kept = [rid for rid in region_ids if sizes[rid] >= MIN_REDUCED]
    print(f"[sbt_scan] regions >= {MIN_REDUCED} reduced-px (~{MIN_REDUCED*SCALE*SCALE} world-blocks): {len(kept):,}", flush=True)

    adjacency_structure = np.array([[0,1,0],[1,1,1],[0,1,0]], dtype=bool)

    def reduced_to_tile(px: int) -> int:
        return (px * SCALE) // TILE_SIZE

    rows = []
    for i, rid in enumerate(kept[:50], 1):
        region = (labeled == rid)
        area_reduced = int(sizes[rid])
        area_blocks = area_reduced * SCALE * SCALE  # approximate blocks

        ys, xs = np.where(region)
        y0, y1 = int(ys.min()), int(ys.max())
        x0, x1 = int(xs.min()), int(xs.max())
        tx0, tx1 = reduced_to_tile(x0), reduced_to_tile(x1)
        tz0, tz1 = reduced_to_tile(y0), reduced_to_tile(y1)

        dilated = ndi.binary_dilation(region, structure=adjacency_structure, iterations=1)
        boundary = dilated & ~region
        neighbour_zones = override_8[boundary]
        neighbour_zones = neighbour_zones[neighbour_zones != SBT_ZONE]
        if len(neighbour_zones) > 0:
            uniq, counts = np.unique(neighbour_zones, return_counts=True)
            order = np.argsort(counts)[::-1]
            top3 = []
            for idx in order[:3]:
                zone = int(uniq[idx])
                cnt = int(counts[idx]) * SCALE  # approximate world-block count
                name = OVERRIDE_BIOME_MAP.get(zone, f"zone-{zone}") or "(water/ocean)"
                top3.append(f"{name} (~{cnt:,}px)")
            adjoining = "; ".join(top3)
        else:
            adjoining = "(none)"

        rows.append({
            "rank": i,
            "area_blocks": area_blocks,
            "area_km2": area_blocks / 1_000_000.0,
            "tile_x_range": f"{tx0}-{tx1}",
            "tile_z_range": f"{tz0}-{tz1}",
            "bbox_world_px": f"({x0*SCALE},{y0*SCALE})-({x1*SCALE},{y1*SCALE})",
            "adjoining": adjoining,
        })
        if i <= 10 or i % 10 == 0:
            print(f"[sbt_scan]   region {i:3d}: ~{area_blocks:>10,} blocks  tiles x={tx0}-{tx1} z={tz0}-{tz1}  adj={adjoining}", flush=True)

    OUT_PATH.parent.mkdir(exist_ok=True)
    lines = [
        "# SBT Presence Report",
        "",
        f"Generated from `{MASK_PATH.name}` scan (50k→1:8 connected components).",
        f"Total zone-35 (SNOWY_BOREAL_TAIGA) pixels at full res: **{sbt_total:,}** ({sbt_total/50000/50000*100:.2f}% of world).",
        f"Connected regions (1:8 scale, all sizes): **{n_regions:,}**.",
        f"Regions ≥ ~{MIN_REDUCED*SCALE*SCALE} world-blocks reported below: **{len(kept):,}** (top 50 shown).",
        "",
        "Note: area values are approximate because connected-components ran at 1:8 scale. "
        "Top-3 neighbour px counts are boundary pixels at 1:8 multiplied by 8 for rough world-pixel scale.",
        "",
        "| # | Area (blocks) | Area (km²) | Tile X range | Tile Z range | Bounding box (world px) | Top 3 adjoining biomes |",
        "|---|---------------|-----------|-------------|-------------|------------------------|-----------------------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['rank']} | {r['area_blocks']:,} | {r['area_km2']:.3f} | "
            f"{r['tile_x_range']} | {r['tile_z_range']} | {r['bbox_world_px']} | {r['adjoining']} |"
        )
    if len(kept) > 50:
        lines.append("")
        lines.append(f"...and {len(kept)-50} smaller regions ≥ {MIN_REGION_PX}px not listed.")

    total_adj_counts: dict[int, int] = {}
    for rid in kept:
        region = (labeled == rid)
        dilated = ndi.binary_dilation(region, structure=adjacency_structure, iterations=1)
        boundary = dilated & ~region
        for zone in override_8[boundary]:
            z = int(zone)
            if z == SBT_ZONE:
                continue
            total_adj_counts[z] = total_adj_counts.get(z, 0) + 1

    lines.append("")
    lines.append("## Aggregate adjoining-biome counts (all reported regions combined)")
    lines.append("")
    lines.append("| Biome | Boundary pixels |")
    lines.append("|-------|-----------------|")
    for zone, cnt in sorted(total_adj_counts.items(), key=lambda x: -x[1]):
        name = OVERRIDE_BIOME_MAP.get(zone, f"zone-{zone}") or "(water/ocean)"
        lines.append(f"| {name} | {cnt:,} |")

    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"[sbt_scan] report written to {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
