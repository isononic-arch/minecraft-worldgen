# Biome Validator Checklist

Auto-generated. For each biome, `best tile` = tile with most pixels of that biome in `override.tif`.

Instructions: walk each biome in-world via the TP command. Mark columns Y/N.

**S85 verdict (2026-05-22):** All present biomes validated good by user. RIPARIAN_WOODLAND + FRESHWATER_FEN absent from world (carry-forward — prune routing if not introduced before next bake).

| Biome | Zone | Best tile | Pure % | TP | Visually OK | GC OK | Schematics OK | Palette OK | Notes |
|-------|------|-----------|--------|----|-------------|-------|---------------|------------|-------|
| COASTAL_HEATH | 10 | (36,7) | 100.0% | `/tp @s 18688 90 3840` | [x] | [x] | [x] | [x] |  |
| TEMPERATE_RAINFOREST | 20 | (19,23) | 100.0% | `/tp @s 9984 180 12032` | [x] | [x] | [x] | [x] |  |
| BOREAL_TAIGA | 30 | (26,10) | 100.0% | `/tp @s 13568 200 5376` | [x] | [x] | [x] | [x] |  |
| SNOWY_BOREAL_TAIGA | 35 | (26,20) | 100.0% | `/tp @s 13568 220 10496` | [x] | [x] | [x] | [x] |  |
| BOREAL_ALPINE | 40 | (86,51) | 100.0% | `/tp @s 44288 250 26368` | [x] | [x] | [x] | [x] |  |
| ARCTIC_TUNDRA | 50 | (32,10) | 100.0% | `/tp @s 16640 180 5376` | [x] | [x] | [x] | [x] |  |
| FROZEN_FLATS | 55 | (31,3) | 100.0% | `/tp @s 16128 160 1792` | [x] | [x] | [x] | [x] |  |
| TEMPERATE_DECIDUOUS | 60 | (41,35) | 100.0% | `/tp @s 21248 180 18176` | [x] | [x] | [x] | [x] |  |
| RAINFOREST_COAST | 70 | (14,50) | 99.6% | `/tp @s 7424 90 25856` | [x] | [x] | [x] | [x] |  |
| RIPARIAN_WOODLAND | 80 | (absent) | 0.0% | `(biome not present)` | [ ] | [ ] | [ ] | [ ] | absent from world |
| DRY_OAK_SAVANNA | 90 | (33,49) | 100.0% | `/tp @s 17152 130 25344` | [x] | [x] | [x] | [x] |  |
| KARST_BARRENS | 100 | (34,9) | 100.0% | `/tp @s 17664 160 4864` | [x] | [x] | [x] | [x] |  |
| BIRCH_FOREST | 110 | (20,36) | 100.0% | `/tp @s 10496 180 18688` | [x] | [x] | [x] | [x] |  |
| EASTERN_TEMPERATE_COAST | 115 | (72,92) | 50.4% | `/tp @s 37120 90 47360` | [x] | [x] | [x] | [x] |  |
| MIXED_FOREST | 120 | (50,48) | 100.0% | `/tp @s 25856 200 24832` | [x] | [x] | [x] | [x] |  |
| CONTINENTAL_STEPPE | 130 | (38,11) | 100.0% | `/tp @s 19712 140 5888` | [x] | [x] | [x] | [x] |  |
| DRY_PINE_BARRENS | 140 | (92,50) | 100.0% | `/tp @s 47360 140 25856` | [x] | [x] | [x] | [x] |  |
| SCRUBBY_HEATHLAND | 150 | (86,78) | 100.0% | `/tp @s 44288 140 40192` | [x] | [x] | [x] | [x] |  |
| LUSH_RAINFOREST_COAST | 160 | (11,64) | 100.0% | `/tp @s 5888 90 33024` | [x] | [x] | [x] | [x] |  |
| SAND_DUNE_DESERT | 170 | (17,66) | 100.0% | `/tp @s 8960 120 34048` | [x] | [x] | [x] | [x] |  |
| DESERT_STEPPE_TRANSITION | 190 | (18,62) | 100.0% | `/tp @s 9472 120 32000` | [x] | [x] | [x] | [x] |  |
| SEMI_ARID_SHRUBLAND | 200 | (86,75) | 100.0% | `/tp @s 44288 140 38656` | [x] | [x] | [x] | [x] |  |
| DRY_WOODLAND_MAQUIS | 210 | (36,75) | 100.0% | `/tp @s 18688 140 38656` | [x] | [x] | [x] | [x] |  |
| TIDAL_JUNGLE_FRINGE | 220 | (43,89) | 100.0% | `/tp @s 22272 90 45824` | [x] | [x] | [x] | [x] |  |
| MANGROVE_COAST | 230 | (32,89) | 55.4% | `/tp @s 16640 80 45824` | [x] | [x] | [x] | [x] |  |
| FRESHWATER_FEN | 240 | (absent) | 0.0% | `(biome not present)` | [ ] | [ ] | [ ] | [ ] | absent from world |

## Legend
- **Visually OK**: overall in-world first impression matches the intent.
- **GC OK**: ground cover density + species mix looks right for the biome.
- **Schematics OK**: trees/bushes placed at correct density + sitting on ground.
- **Palette OK**: surface + subsurface blocks read as the right geology.
- **Notes**: anything worth a follow-up NICK PRIORITIES entry.