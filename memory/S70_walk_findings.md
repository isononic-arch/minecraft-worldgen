# S70 Biome Walk — Findings (2026-04-25)

User-walked, in-world. Source: 33-tile render batch shipped to `Vandirtest10/region/`.

## Cross-cutting bugs (multi-biome impact)

### B1. Leaves-pulled-underground on small trees
- **Affected biomes:** DRY_OAK_SAVANNA (33,49), DRY_PINE_BARRENS (92,50), DESERT_STEPPE_TRANSITION (18,62), DRY_WOODLAND_MAQUIS (36,75)
- **Symptom:** small trees appear as "columns of leaves" — trunk pulled below surface, only canopy visible.
- **User direction:** the schem-modifier that sinks blocks should apply ONLY to log/wood blocks, not leaves. Either (a) restrict the sink to log blocks, or (b) reclassify these schematics so they don't go through the bush-sink path.
- **Pipeline reference:** S61 added bush placement-level sink ("`bush = placement-level sink if max_gap ≤ 3`" per CLAUDE.md).

### B2. Biome MC-mapping mismatches
- **ARCTIC_TUNDRA (32,10):** rendering as if it were SNOWY_BOREAL_TAIGA palette. Hella trees, should be scrubland.
- **SNOWY_BOREAL_TAIGA (26,20):** rendering as non-snowy boreal taiga palette. Should be `minecraft:taiga` MC biome + snow cover.
- **CONTINENTAL_STEPPE (38,11):** looks like alpine boreal, not steppe. User suspects map-vs-pipeline biome mismatch.
- **DRY_WOODLAND_MAQUIS (36,75):** marked as jungle (bright green leaves) — should be less vibrant since it's a DRY biome.
- **Suspicion:** swapped or shifted-by-one mapping in `BIOME_TO_MC_*` dicts in `core/chunk_writer.py`.

### B3. River drainage / connectivity
- **ARCTIC_TUNDRA (32,10):** river on a hill — water flows sideways. **Rule:** rivers must always be below their nearest surface Y.
- **TEMPERATE_DECIDUOUS (41,35):** river delta fan exists but does not actually connect to ocean.
- **DRY_WOODLAND_MAQUIS (36,75):** river delta also does not connect to water.

### B4. Reference tiles in ocean (no land surface to validate)
- **EASTERN_TEMPERATE_COAST (72,92)** — generated in ocean, Y values not accounting for ocean layer.
- **TIDAL_JUNGLE_FRINGE (43,89)** — generated in ocean.
- **SCRUBBY_HEATHLAND (86,78)** — generated in ocean.
- **SEMI_ARID_SHRUBLAND (86,75)** — ocean tile.
- **Root cause:** `tools/diag_biome_sampler.py` picked the tile with the most pixels of a given zone, but didn't filter for elevation > sea level. Need re-pick on land.

## Per-biome palette / density tweaks

### SNOWY_BOREAL_TAIGA (26,20)
- Should map to `minecraft:taiga` biome.
- Should have snow cover.

### ARCTIC_TUNDRA (32,10)
- Should be more scrubland, not trees.
- Surface = mix of snow + existing dirt palette.

### FROZEN_FLATS (31,3)
- Patches of bare coarse dirt + gravel — should be snow blocks instead.
- Snow carpet layer is uneven, looks messy. Make even.
- Increase bush schematic frequency (still sparse).
- Add dead short grass + dead tall grass.

### COASTAL_HEATH (36,7)
- Flowers way too dense — make very sparse.
- Double bush schematic density.
- Sand should be `suspicious_sand` (different texture from `sand`).

### RAINFOREST_COAST (14,50)
- Get rid of palm trees.
- Reduce mud amount.

### MANGROVE_COAST (32,89)
- A few trees have floating roots (log/wood blocks float). Schem editor fix.
- Bizarre dune geometry at the coast — there should not be sand dunes here at all.

### DRY_OAK_SAVANNA (33,49)
- Reduce flowers by half.
- Leaves-underground bug (see B1).

### DRY_PINE_BARRENS (92,50)
- Mostly good.
- Leaves-underground bug (see B1).

### DESERT_STEPPE_TRANSITION (18,62)
- Leaves-underground bug (see B1).

### DRY_WOODLAND_MAQUIS (36,75)
- Currently rendering as jungle (vibrant green) — should be less vibrant for a dry biome.
- Leaves-underground bug (see B1).
- River delta doesn't connect to water (see B3).
- Note: world map shows no river at this tile despite render — may be a tile-coord mismatch or aggressive prune ate the painted river.

### FRESHWATER_FEN (8,74)
- Gorgeous overall.
- Floodplain-overwriting-trees rule should make an exception here. See RIPARIAN_WOODLAND for full reasoning.

### RIPARIAN_WOODLAND (73,53)
- Floodplains currently overwrite riparian woodland — biome change is visible but very few riparian groves spawn because floodplain eats them.
- **User direction:** make floodplain an exception in RIPARIAN_WOODLAND + FRESHWATER_FEN. Trees should survive.

### LUSH_RAINFOREST_COAST (11,64)
- Reduce palm count overall.
- Palms should ONLY generate very close to the shoreline (not interior).

### KARST_BARRENS (34,9)
- Cluster bushes into groves rather than evenly-distributed random sprinkle. More clumpy.
- Increase short_grass density, reduce dead_grass (short + tall) density. Currently dead-heavy.

### SAND_DUNE_DESERT (17,66)
- Dune-to-DESERT_STEPPE_TRANSITION boundary looks good.
- BUT: no bush schematics, no dead grass, no ground cover — just sand blocks.
- **Likely cause:** sand_dune gap (8) overrides meadow gap (1), and ground cover spawns on gap==1. So in dunes the ground-cover pass is skipped entirely. Need to allow specific schematics (bush) + GC species (dead grass) on gap==8 within SAND_DUNE_DESERT.

## Continental steppe family follow-up
- Continental_steppe AND alpine_boreal look very similar in-world.
- Continental steppe should have NO or VERY SPARSE trees — currently has many.
- This is downstream of B2 (biome mismatch) — possibly the same swap.

## User answers (Q1–Q4)
- **Q1 (small trees):** Confirmed — only smaller trees. Stamp schematic bug, scoped to bush-vs-tree classification path.
- **Q2 (palette vs MC biome):** Both. ARCTIC_TUNDRA reads as `snowy_taiga` in F3 (wrong MC mapping) AND vegetation palette is wrong (looks like SNOWY_BOREAL_TAIGA's palette). Ground/schematics under the snow do match snowy_taiga's expected state.
- **Q3 (river-on-hill):** Flowing sideways visibly. The channel centerline correctly follows the hill's downhill direction, BUT the water surface has a slope ACROSS THE WIDTH of the channel — one bank higher than the other. Real rivers have flat water surface across width. User has screenshots in OneDrive.
- **Q4 (DRY_WOODLAND_MAQUIS river):** Stick with hydro tab paint as source of truth.

## Biomes that walked clean (no flagged issues)
TEMPERATE_RAINFOREST, BOREAL_TAIGA, BOREAL_ALPINE, BIRCH_FOREST, MIXED_FOREST, TEMPERATE_DECIDUOUS (besides river issue + tile coverage).

## Open questions for user
See main session for active Q&A before plan mode.

---

## S70 follow-up walk findings (post-f5b, 2026-04-26)

### Resolved by code fix
- ✅ KARST_BARRENS — clustering [0.0,2.5] gives groves
- ✅ LUSH_RAINFOREST_COAST — denser canopy, palms only near shore, no floodplain mud
- ✅ RIPARIAN_WOODLAND — trees survive (no floodplain wipe)
- ✅ Coastal slivers (orange near LUSH coast etc.) — coastal-zone repaint pass

### Partially fixed
- ⚠ DRY_OAK_SAVANNA — Plan B trunk threshold (0.85) helps but `_b_sm`/`_f_lg` schematics still have "appendage" leaves visible.  User suggested editing the .schem files directly via schem_viewer.

### Pending validation (last render shipped 17:50 Apr 26 with f5b fixes)
- 🔄 SAND_DUNE_DESERT — `_NON_PLANTABLE` sand-fix + 0.20 eco_density_mod + cactus restored.  USER WALK NEEDED.
- 🔄 Adjacent dry biomes (DESERT_STEPPE_TRANSITION, DRY_PINE_BARRENS, DRY_WOODLAND_MAQUIS) — TRUNK_RUN_FRAC=0.85 should help but not re-rendered yet with f5b.

### Known not-yet-addressed (carry to next session)
- River-tilt fix (Item P) not validated in-world.  Walk a hilly river tile to verify.
- Trees overhanging rivers — code path attempted then reverted.  Schematic placement still rejects tree footprints with any underwater column.
- River delta-doesn't-connect-to-ocean (TEMPERATE_DECIDUOUS, DRY_WOODLAND_MAQUIS) — separate from water-tilt; deferred.
- MANGROVE_COAST schematic floating roots — user editing in schem editor.
- High-tundra altitude no vegetation (ARCTIC_TUNDRA observation) — may be by design (altitude cutoff); revisit if user cares.

### S70 commit pending — files in `memory/S70_handoff.md`
