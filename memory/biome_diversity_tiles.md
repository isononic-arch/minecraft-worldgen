# Biome Diversity Tile Finder (S62)

Scan of `masks/override.tif` at 1:8 resolution. Tile = 512×512 blocks = 64×64 px at 1:8.

**Threshold:** biome counted as "present in tile" if >=5.0% tile coverage (>=200 1:8 px).

## Present biomes summary

- **Covered:** 26 biomes
- **Absent / too sparse:** 0 — []

## Greedy set-cover walk list

Minimum **8 tiles** cover every present biome.

| # | Tile | New biomes covered | Total biomes in tile | Litho groups | TP (center) |
|---|------|-------------------|----------------------|--------------|-------------|
| 1 | (20,53) | BOREAL_ALPINE, TEMPERATE_DECIDUOUS, DRY_OAK_SAVANNA, BIRCH_FOREST, SEMI_ARID_SHRUBLAND | 5 | arid_basaltic, deepslate_metamorphic, granitic | `/tp @s 10496 200 27392` |
| 2 | (34,93) | LUSH_RAINFOREST_COAST, DRY_WOODLAND_MAQUIS, TIDAL_JUNGLE_FRINGE, MANGROVE_COAST | 4 | arid_basaltic, limestone, mossy_temperate, temperate_basaltic | `/tp @s 17664 200 47872` |
| 3 | (32,7) | COASTAL_HEATH, ARCTIC_TUNDRA, FROZEN_FLATS, KARST_BARRENS | 4 | deepslate_metamorphic, granitic, limestone | `/tp @s 16640 200 3840` |
| 4 | (49,35) | BOREAL_TAIGA, EASTERN_TEMPERATE_COAST, CONTINENTAL_STEPPE | 4 | arid_basaltic, granitic, temperate_basaltic | `/tp @s 25344 200 18176` |
| 5 | (11,77) | SNOWY_BOREAL_TAIGA, SAND_DUNE_DESERT, DESERT_STEPPE_TRANSITION | 4 | arid_basaltic, deepslate_metamorphic | `/tp @s 5888 200 39680` |
| 6 | (84,84) | RAINFOREST_COAST, MIXED_FOREST, SCRUBBY_HEATHLAND | 3 | arid_basaltic, granitic, mossy_temperate | `/tp @s 43264 200 43264` |
| 7 | (8,73) | TEMPERATE_RAINFOREST, FRESHWATER_FEN | 3 | arid_basaltic, mossy_temperate | `/tp @s 4352 200 37632` |
| 8 | (84,67) | RIPARIAN_WOODLAND, DRY_PINE_BARRENS | 3 | arid_basaltic | `/tp @s 43264 200 34560` |

## Top-30 most diverse tiles (regardless of set cover)

| Rank | Tile | # biomes | Biomes (pct) | Litho groups |
|------|------|----------|--------------|--------------|
| 1 | (20,53) | 5 | TEMPERATE_DECIDUOUS(38%), BIRCH_FOREST(17%), SEMI_ARID_SHRUBLAND(17%), DRY_OAK_SAVANNA(17%), BOREAL_ALPINE(11%) | arid_basaltic, deepslate_metamorphic, granitic |
| 2 | (27,47) | 4 | BIRCH_FOREST(75%), SEMI_ARID_SHRUBLAND(12%), SCRUBBY_HEATHLAND(6%), TEMPERATE_DECIDUOUS(6%) | arid_basaltic, granitic |
| 3 | (34,93) | 4 | LUSH_RAINFOREST_COAST(72%), DRY_WOODLAND_MAQUIS(10%), TIDAL_JUNGLE_FRINGE(9%), MANGROVE_COAST(5%) | arid_basaltic, limestone, mossy_temperate, temperate_basaltic |
| 4 | (20,52) | 4 | DRY_OAK_SAVANNA(65%), SEMI_ARID_SHRUBLAND(18%), TEMPERATE_DECIDUOUS(9%), BIRCH_FOREST(8%) | arid_basaltic, granitic |
| 5 | (21,46) | 4 | BOREAL_TAIGA(65%), BOREAL_ALPINE(14%), TEMPERATE_DECIDUOUS(11%), SNOWY_BOREAL_TAIGA(10%) | deepslate_metamorphic, granitic |
| 6 | (30,52) | 4 | DRY_OAK_SAVANNA(64%), SEMI_ARID_SHRUBLAND(20%), BOREAL_ALPINE(8%), BIRCH_FOREST(7%) | arid_basaltic, deepslate_metamorphic, granitic |
| 7 | (26,80) | 4 | BOREAL_ALPINE(62%), SNOWY_BOREAL_TAIGA(21%), DRY_OAK_SAVANNA(11%), ARCTIC_TUNDRA(6%) | arid_basaltic, deepslate_metamorphic |
| 8 | (29,52) | 4 | DRY_OAK_SAVANNA(59%), SEMI_ARID_SHRUBLAND(23%), BOREAL_ALPINE(9%), BIRCH_FOREST(9%) | arid_basaltic, deepslate_metamorphic, granitic |
| 9 | (32,7) | 4 | KARST_BARRENS(59%), COASTAL_HEATH(21%), FROZEN_FLATS(15%), ARCTIC_TUNDRA(5%) | deepslate_metamorphic, granitic, limestone |
| 10 | (49,35) | 4 | TEMPERATE_DECIDUOUS(58%), BOREAL_TAIGA(26%), EASTERN_TEMPERATE_COAST(9%), CONTINENTAL_STEPPE(5%) | arid_basaltic, granitic, temperate_basaltic |
| 11 | (27,86) | 4 | BOREAL_ALPINE(54%), DRY_WOODLAND_MAQUIS(23%), SEMI_ARID_SHRUBLAND(13%), DRY_OAK_SAVANNA(10%) | arid_basaltic, deepslate_metamorphic, limestone |
| 12 | (59,90) | 4 | MIXED_FOREST(49%), EASTERN_TEMPERATE_COAST(16%), LUSH_RAINFOREST_COAST(14%), DRY_PINE_BARRENS(9%) | arid_basaltic, granitic, mossy_temperate, temperate_basaltic |
| 13 | (11,77) | 4 | BOREAL_ALPINE(48%), SAND_DUNE_DESERT(27%), DESERT_STEPPE_TRANSITION(19%), SNOWY_BOREAL_TAIGA(5%) | arid_basaltic, deepslate_metamorphic |
| 14 | (27,88) | 4 | DRY_WOODLAND_MAQUIS(47%), MANGROVE_COAST(18%), SEMI_ARID_SHRUBLAND(9%), TIDAL_JUNGLE_FRINGE(7%) | arid_basaltic, limestone, temperate_basaltic |
| 15 | (37,22) | 4 | SNOWY_BOREAL_TAIGA(47%), CONTINENTAL_STEPPE(23%), KARST_BARRENS(23%), BOREAL_TAIGA(7%) | arid_basaltic, deepslate_metamorphic, granitic, limestone |
| 16 | (84,81) | 4 | BOREAL_ALPINE(46%), MIXED_FOREST(32%), CONTINENTAL_STEPPE(12%), TEMPERATE_RAINFOREST(6%) | arid_basaltic, deepslate_metamorphic, granitic, mossy_temperate |
| 17 | (45,87) | 4 | DRY_WOODLAND_MAQUIS(45%), TIDAL_JUNGLE_FRINGE(29%), EASTERN_TEMPERATE_COAST(10%), MANGROVE_COAST(10%) | arid_basaltic, limestone, temperate_basaltic |
| 18 | (31,45) | 4 | BIRCH_FOREST(44%), SEMI_ARID_SHRUBLAND(36%), TEMPERATE_DECIDUOUS(8%), EASTERN_TEMPERATE_COAST(7%) | arid_basaltic, granitic, temperate_basaltic |
| 19 | (19,52) | 4 | BOREAL_ALPINE(41%), BOREAL_TAIGA(36%), TEMPERATE_DECIDUOUS(14%), BIRCH_FOREST(5%) | deepslate_metamorphic, granitic |
| 20 | (21,52) | 4 | DRY_OAK_SAVANNA(41%), TEMPERATE_DECIDUOUS(27%), SEMI_ARID_SHRUBLAND(19%), BIRCH_FOREST(14%) | arid_basaltic, granitic |
| 21 | (40,26) | 4 | CONTINENTAL_STEPPE(40%), KARST_BARRENS(29%), BOREAL_TAIGA(23%), SNOWY_BOREAL_TAIGA(9%) | arid_basaltic, deepslate_metamorphic, granitic, limestone |
| 22 | (18,45) | 4 | BOREAL_ALPINE(40%), TEMPERATE_RAINFOREST(32%), SNOWY_BOREAL_TAIGA(14%), BOREAL_TAIGA(14%) | deepslate_metamorphic, granitic, mossy_temperate |
| 23 | (27,49) | 4 | BOREAL_ALPINE(38%), DRY_OAK_SAVANNA(38%), SEMI_ARID_SHRUBLAND(18%), BOREAL_TAIGA(7%) | arid_basaltic, deepslate_metamorphic, granitic |
| 24 | (87,73) | 4 | SCRUBBY_HEATHLAND(37%), DRY_PINE_BARRENS(32%), SEMI_ARID_SHRUBLAND(22%), CONTINENTAL_STEPPE(9%) | arid_basaltic |
| 25 | (38,23) | 4 | BOREAL_TAIGA(37%), SNOWY_BOREAL_TAIGA(27%), KARST_BARRENS(25%), CONTINENTAL_STEPPE(11%) | arid_basaltic, deepslate_metamorphic, granitic, limestone |
| 26 | (53,50) | 4 | TEMPERATE_RAINFOREST(36%), MIXED_FOREST(26%), BIRCH_FOREST(26%), BOREAL_TAIGA(10%) | granitic, mossy_temperate |
| 27 | (62,51) | 4 | SNOWY_BOREAL_TAIGA(36%), BIRCH_FOREST(31%), BOREAL_TAIGA(28%), BOREAL_ALPINE(6%) | deepslate_metamorphic, granitic |
| 28 | (24,45) | 4 | BOREAL_TAIGA(33%), SNOWY_BOREAL_TAIGA(25%), TEMPERATE_DECIDUOUS(23%), BOREAL_ALPINE(19%) | deepslate_metamorphic, granitic |
| 29 | (22,51) | 4 | SNOWY_BOREAL_TAIGA(33%), BOREAL_ALPINE(31%), DRY_OAK_SAVANNA(30%), BOREAL_TAIGA(6%) | arid_basaltic, deepslate_metamorphic, granitic |
| 30 | (45,94) | 4 | DRY_WOODLAND_MAQUIS(32%), MANGROVE_COAST(23%), TIDAL_JUNGLE_FRINGE(20%), LUSH_RAINFOREST_COAST(9%) | arid_basaltic, limestone, mossy_temperate, temperate_basaltic |

## Per-biome best-multibiome tiles

For each biome, the 3 most-diverse tiles containing it (>=10% coverage of that biome).

| Biome | Zone | Best 3 tiles (tx,tz / biomes in tile) |
|-------|------|----------------------------------------|
| COASTAL_HEATH | 10 | (32,7)/4bio; (26,8)/3bio; (24,18)/3bio |
| TEMPERATE_RAINFOREST | 20 | (18,45)/4bio; (53,50)/4bio; (77,75)/3bio |
| BOREAL_TAIGA | 30 | (21,46)/4bio; (49,35)/4bio; (19,52)/4bio |
| SNOWY_BOREAL_TAIGA | 35 | (21,46)/4bio; (26,80)/4bio; (37,22)/4bio |
| BOREAL_ALPINE | 40 | (20,53)/5bio; (21,46)/4bio; (26,80)/4bio |
| ARCTIC_TUNDRA | 50 | (77,72)/3bio; (26,18)/3bio; (62,54)/3bio |
| FROZEN_FLATS | 55 | (32,7)/4bio; (32,6)/3bio; (30,5)/2bio |
| TEMPERATE_DECIDUOUS | 60 | (20,53)/5bio; (21,46)/4bio; (49,35)/4bio |
| RAINFOREST_COAST | 70 | (23,57)/4bio; (33,60)/3bio; (27,57)/3bio |
| RIPARIAN_WOODLAND | 80 | (71,50)/3bio; (81,51)/3bio; (79,51)/3bio |
| DRY_OAK_SAVANNA | 90 | (20,53)/5bio; (20,52)/4bio; (30,52)/4bio |
| KARST_BARRENS | 100 | (32,7)/4bio; (37,22)/4bio; (40,26)/4bio |
| BIRCH_FOREST | 110 | (20,53)/5bio; (27,47)/4bio; (31,45)/4bio |
| EASTERN_TEMPERATE_COAST | 115 | (59,90)/4bio; (45,87)/4bio; (21,38)/3bio |
| MIXED_FOREST | 120 | (59,90)/4bio; (84,81)/4bio; (53,50)/4bio |
| CONTINENTAL_STEPPE | 130 | (37,22)/4bio; (84,81)/4bio; (40,26)/4bio |
| DRY_PINE_BARRENS | 140 | (87,73)/4bio; (59,89)/3bio; (90,52)/3bio |
| SCRUBBY_HEATHLAND | 150 | (87,73)/4bio; (86,77)/3bio; (84,84)/3bio |
| LUSH_RAINFOREST_COAST | 160 | (34,93)/4bio; (59,90)/4bio; (23,57)/4bio |
| SAND_DUNE_DESERT | 170 | (11,77)/4bio; (16,69)/3bio; (18,64)/3bio |
| DESERT_STEPPE_TRANSITION | 190 | (11,77)/4bio; (17,61)/3bio; (18,64)/3bio |
| SEMI_ARID_SHRUBLAND | 200 | (20,53)/5bio; (27,47)/4bio; (20,52)/4bio |
| DRY_WOODLAND_MAQUIS | 210 | (34,93)/4bio; (27,86)/4bio; (27,88)/4bio |
| TIDAL_JUNGLE_FRINGE | 220 | (45,87)/4bio; (45,94)/4bio; (42,94)/3bio |
| MANGROVE_COAST | 230 | (27,88)/4bio; (45,87)/4bio; (45,94)/4bio |
| FRESHWATER_FEN | 240 | (8,74)/3bio; (30,89)/3bio |
