"""
S62 biome-diversity tile finder.

Scan masks/override.tif at 1:8 resolution, count biome zones per 512×512 tile,
and run greedy set-cover to find the minimum tile list that covers every
present biome with >=5% tile coverage.

Also tags tiles by lithology groups present (for palette validation coverage).

Output: memory/biome_diversity_tiles.md
"""

from __future__ import annotations
import numpy as np
import rasterio
from rasterio.enums import Resampling
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OVERRIDE_TIF = REPO_ROOT / "masks" / "override.tif"
OUTPUT_MD    = REPO_ROOT / "memory" / "biome_diversity_tiles.md"

ZONE_TO_NAME = {
    0:   "(none)",
    10:  "COASTAL_HEATH",
    20:  "TEMPERATE_RAINFOREST",
    30:  "BOREAL_TAIGA",
    35:  "SNOWY_BOREAL_TAIGA",
    40:  "BOREAL_ALPINE",
    50:  "ARCTIC_TUNDRA",
    55:  "FROZEN_FLATS",
    60:  "TEMPERATE_DECIDUOUS",
    70:  "RAINFOREST_COAST",
    80:  "RIPARIAN_WOODLAND",
    90:  "DRY_OAK_SAVANNA",
    100: "KARST_BARRENS",
    110: "BIRCH_FOREST",
    115: "EASTERN_TEMPERATE_COAST",
    120: "MIXED_FOREST",
    130: "CONTINENTAL_STEPPE",
    140: "DRY_PINE_BARRENS",
    150: "SCRUBBY_HEATHLAND",
    160: "LUSH_RAINFOREST_COAST",
    170: "SAND_DUNE_DESERT",
    190: "DESERT_STEPPE_TRANSITION",
    200: "SEMI_ARID_SHRUBLAND",
    210: "DRY_WOODLAND_MAQUIS",
    220: "TIDAL_JUNGLE_FRINGE",
    230: "MANGROVE_COAST",
    240: "FRESHWATER_FEN",
}

ZONE_TO_LITHO = {
    10: "granitic", 20: "mossy_temperate", 30: "granitic", 35: "deepslate_metamorphic",
    40: "deepslate_metamorphic", 50: "deepslate_metamorphic", 55: "deepslate_metamorphic",
    60: "granitic", 70: "mossy_temperate", 80: "arid_basaltic", 90: "arid_basaltic",
    100: "limestone", 110: "granitic", 115: "temperate_basaltic", 120: "granitic",
    130: "arid_basaltic", 140: "arid_basaltic", 150: "arid_basaltic",
    160: "mossy_temperate", 170: "arid_basaltic", 190: "arid_basaltic",
    200: "arid_basaltic", 210: "limestone", 220: "temperate_basaltic",
    230: "arid_basaltic", 240: "arid_basaltic",
}

TILE_PX_1_8  = 64        # 512 / 8 = 64 px per tile at 1:8
N_TILES      = 97
MIN_PX_1_8   = 200       # >=5% of 4096 = 204.8, round to 200 (1:8)
MIN_PCT      = 5.0       # report threshold

print(f"Loading override.tif at 1:8 resolution (6250×6250 expected)...", flush=True)
with rasterio.open(OVERRIDE_TIF) as src:
    h8 = src.height // 8
    w8 = src.width  // 8
    print(f"  full: {src.height}×{src.width}, 1:8: {h8}×{w8}", flush=True)
    data = src.read(1, out_shape=(h8, w8), resampling=Resampling.nearest)

print(f"Data shape: {data.shape}, dtype {data.dtype}, unique zones: {np.unique(data)}", flush=True)

# Tile diversity scan.
records = []
for tz in range(N_TILES):
    for tx in range(N_TILES):
        tile = data[tz*TILE_PX_1_8:(tz+1)*TILE_PX_1_8, tx*TILE_PX_1_8:(tx+1)*TILE_PX_1_8]
        unique, counts = np.unique(tile, return_counts=True)
        biomes = []
        for u, c in zip(unique, counts):
            if u == 0:
                continue
            if c < MIN_PX_1_8:
                continue
            pct = c / (TILE_PX_1_8 * TILE_PX_1_8) * 100.0
            biomes.append((int(u), int(c), pct))
        if biomes:
            biomes.sort(key=lambda b: -b[1])
            records.append({
                'tile': (tx, tz),
                'biomes': biomes,
                'n_biomes': len(biomes),
                'litho_groups': sorted(set(ZONE_TO_LITHO.get(b[0], '?') for b in biomes)),
            })

print(f"Tiles with >=1 biome above threshold: {len(records)}", flush=True)

# Sort by biome diversity (most first)
records.sort(key=lambda r: (-r['n_biomes'], -max(b[2] for b in r['biomes'])))

# Present biomes (globally)
present_zones = set()
for r in records:
    for (z, _, _) in r['biomes']:
        present_zones.add(z)
absent_zones = sorted(set(ZONE_TO_NAME.keys()) - present_zones - {0})
print(f"Present biomes >={MIN_PCT}% in some tile: {len(present_zones)}", flush=True)
print(f"Absent (or too-sparse) biomes: {[(z, ZONE_TO_NAME[z]) for z in absent_zones]}", flush=True)

# Greedy set cover on zone codes.
uncovered = set(present_zones)
cover_tiles = []
while uncovered:
    best = None
    best_score = 0
    for r in records:
        zs = set(b[0] for b in r['biomes']) & uncovered
        if len(zs) > best_score:
            best = r
            best_score = len(zs)
    if best is None:
        break
    cover_tiles.append(best)
    for (z, _, _) in best['biomes']:
        uncovered.discard(z)
print(f"Greedy set cover: {len(cover_tiles)} tiles cover all {len(present_zones)} present biomes", flush=True)

# Write report.
with open(OUTPUT_MD, 'w', encoding='utf-8') as f:
    f.write("# Biome Diversity Tile Finder (S62)\n\n")
    f.write(f"Scan of `masks/override.tif` at 1:8 resolution. Tile = 512×512 blocks = 64×64 px at 1:8.\n\n")
    f.write(f"**Threshold:** biome counted as \"present in tile\" if >={MIN_PCT}% tile coverage (>={MIN_PX_1_8} 1:8 px).\n\n")

    f.write("## Present biomes summary\n\n")
    f.write(f"- **Covered:** {len(present_zones)} biomes\n")
    absent_names = [ZONE_TO_NAME[z] for z in absent_zones]
    f.write(f"- **Absent / too sparse:** {len(absent_zones)} — {absent_names}\n\n")

    f.write("## Greedy set-cover walk list\n\n")
    f.write(f"Minimum **{len(cover_tiles)} tiles** cover every present biome.\n\n")
    f.write("| # | Tile | New biomes covered | Total biomes in tile | Litho groups | TP (center) |\n")
    f.write("|---|------|-------------------|----------------------|--------------|-------------|\n")
    covered_so_far = set()
    for i, r in enumerate(cover_tiles, 1):
        tx, tz = r['tile']
        new_zs = sorted(set(b[0] for b in r['biomes']) - covered_so_far)
        new_names = [ZONE_TO_NAME[z] for z in new_zs]
        covered_so_far |= set(b[0] for b in r['biomes'])
        center_x = tx * 512 + 256
        center_z = tz * 512 + 256
        tp = f"/tp @s {center_x} 200 {center_z}"
        new_str = ", ".join(new_names)
        litho_str = ", ".join(r['litho_groups'])
        f.write(f"| {i} | ({tx},{tz}) | {new_str} | {r['n_biomes']} | {litho_str} | `{tp}` |\n")

    f.write("\n## Top-30 most diverse tiles (regardless of set cover)\n\n")
    f.write("| Rank | Tile | # biomes | Biomes (pct) | Litho groups |\n")
    f.write("|------|------|----------|--------------|--------------|\n")
    for i, r in enumerate(records[:30], 1):
        tx, tz = r['tile']
        biomes_str = ", ".join(f"{ZONE_TO_NAME[z]}({p:.0f}%)" for (z, _, p) in r['biomes'])
        litho_str = ", ".join(r['litho_groups'])
        f.write(f"| {i} | ({tx},{tz}) | {r['n_biomes']} | {biomes_str} | {litho_str} |\n")

    f.write("\n## Per-biome best-multibiome tiles\n\n")
    f.write("For each biome, the 3 most-diverse tiles containing it (>=10% coverage of that biome).\n\n")
    f.write("| Biome | Zone | Best 3 tiles (tx,tz / biomes in tile) |\n")
    f.write("|-------|------|----------------------------------------|\n")
    for z in sorted(present_zones):
        hosts = [r for r in records if any(b[0] == z and b[2] >= 10.0 for b in r['biomes'])]
        hosts.sort(key=lambda r: -r['n_biomes'])
        top3 = hosts[:3]
        cell = "; ".join(f"({r['tile'][0]},{r['tile'][1]})/{r['n_biomes']}bio" for r in top3) or "—"
        f.write(f"| {ZONE_TO_NAME[z]} | {z} | {cell} |\n")

print(f"\nReport written: {OUTPUT_MD}", flush=True)
print(f"\n=== SUMMARY ===")
print(f"Present biomes: {len(present_zones)} / 26 total")
print(f"Walk list: {len(cover_tiles)} tiles to cover all")
print(f"Estimated render time: {len(cover_tiles)} × ~18 min = ~{len(cover_tiles) * 18} min")
