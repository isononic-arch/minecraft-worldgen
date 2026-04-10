# Vegetation Mix Specification — All 27 Biomes
*Ground cover, flowers, aquatic plants, bushes — MC 1.21.10 (DataVersion 4556)*
*All blocks verified available in Java Edition 1.21.10*

## Block ID Reference (1.21.10)

### Grasses
- `short_grass` — low grass (renamed from `grass` in 1.20.3)
- `tall_grass` — double-height grass
- `short_dry_grass` — arid grass (1.21.5+)
- `tall_dry_grass` — double-height dry grass (1.21.5+)
- `fern` — forest floor fern
- `large_fern` — double-height fern

### Shrubs & Bushes
- `bush` — generic bush block (1.21.5+) — PRIMARY bush, used broadly
- `dead_bush` — dried shrub
- `sweet_berry_bush` — harvestable berry bush (rare, needs block state age=3)
- `azalea` — temperate shrub
- `flowering_azalea` — flowering variant
- `firefly_bush` — glowing bush (1.21.5+) — EXCEEDINGLY RARE, shiny accent only
- `cactus` — desert column plant (VERY VERY rare)

### Flowers
- `poppy`, `dandelion`, `cornflower`, `allium`, `azure_bluet`, `oxeye_daisy`
- `blue_orchid` — wetland flower
- `lily_of_the_valley` — shade-loving forest flower
- `orange_tulip`, `pink_tulip`, `red_tulip`, `white_tulip`
- `torchflower` — warm climate flower

### Double-Tall Flowers
- `rose_bush` — woodland edge
- `peony` — shade forest
- `lilac` — forest clearing
- `sunflower` — grassland/steppe
- `pitcher_plant` — tropical

### Forest Floor
- `leaf_litter` — fallen leaf debris (1.21.5+) — DOMINANT in forests, canopy-boosted
- `moss_carpet` — thin moss layer
- `pale_moss_carpet` — pale/boreal moss (NOT in tundra — prevents snow fall)
- `hanging_roots` — under rooted dirt / cliff overhangs
- `resin_clump` — conifer resin (VERY RARE — jarring at high density)

### Climbing / Hanging
- `vines` — tropical/temperate wall/tree climber
- `pale_hanging_moss` — boreal hanging strands (1.21.5+)

### Aquatic
- `seagrass` — shallow water floor
- `tall_seagrass` — moderate depth floor
- `kelp` — deep water column
- `lily_pad` — calm water surface
- `sugar_cane` — waterline reeds

---

## Design Notes (from user feedback)

1. **leaf_litter**: Heavily canopy-boosted — density multiplied by canopy_proxy so it clusters where trees are dense. Without tree proximity data at ground cover time, canopy_proxy (moisture_index x soil_depth) is the best statistical proxy.
2. **bush**: More common than fern in lighter forests. In dense forests, fern still dominates (realistic — fern IS the dominant temperate/boreal understory). In open/light forests (birch, deciduous edges), bush overtakes fern.
3. **firefly_bush**: 0.01 or less everywhere. A rare shiny accent, not a feature.
4. **resin_clump**: 0.02 max. Present but subtle.
5. **cactus**: 0.005 in desert. Extremely rare columns.
6. **sweet_berry_bush**: Included at very low density. Block state handling deferred if complex.
7. **pale_moss_carpet**: NOT in Arctic Tundra (prevents snow accumulation on those blocks).
8. **short_grass**: Bumped up in sparse/heath/grassland biomes for texture variation.
9. **Mangrove roots**: Deferred — complex block placement around tree schematics.

---

## BOREAL / COLD BIOMES

### BOREAL_TAIGA
Character: Dense conifer understory. Fern-dominated but with bushes in clearings. Resin rare.
```
fern                0.35   # dominant shade understory
large_fern          0.15   # deep shade patches
leaf_litter         0.25   # conifer needle litter
moss_carpet         0.15   # mossy patches under trees
pale_moss_carpet    0.06   # boreal pale moss in shade
bush                0.14   # clearings and edges
sweet_berry_bush    0.03   # rare berry patches
short_grass         0.12   # light gaps
resin_clump         0.02   # very rare conifer resin
dead_bush           0.02   # occasional deadfall
```

### SNOWY_BOREAL_TAIGA
Character: Sparse. Mostly bare snow/podzol. Hardy plants in sheltered spots.
```
fern                0.05   # sheltered spots only
leaf_litter         0.04   # sparse needle litter
bush                0.05   # rare hardy shrub
short_grass         0.04   # rare
dead_bush           0.03   # frozen deadfall
resin_clump         0.01   # frozen resin, very rare
```

### ALPINE_MEADOW
Character: Wildflower meadow above treeline. Maximum flower diversity. Lots of short grass texture.
```
short_grass         0.45   # dominant alpine grass — texture base
tall_grass          0.20   # taller tussocks in hollows
bush                0.08   # low alpine scrub
cornflower          0.08   # blue alpine wildflower
allium              0.06   # purple clusters
oxeye_daisy         0.07   # white patches
azure_bluet         0.05   # delicate blue-white
dandelion           0.06   # yellow scatter
poppy               0.04   # red accent
pink_tulip          0.03   # alpine pink
white_tulip         0.02   # rare white
```

### ARCTIC_TUNDRA
Character: Almost barren. Wind-scoured. No moss carpet (blocks snow).
```
dead_bush           0.05   # wind-dried remnants
short_dry_grass     0.04   # sparse tough tufts
short_grass         0.02   # rare sheltered patches
```

### FROZEN_FLATS
Character: Lifeless ice sheet.
```
(empty — no ground cover)
```

---

## TEMPERATE FORESTS

### TEMPERATE_DECIDUOUS
Character: Rich understory. Leaf litter dominates floor (canopy-boosted). Shade flowers. Bushes throughout.
```
leaf_litter         0.35   # dominant — canopy-boosted, clusters near trees
short_grass         0.22   # moderate ground cover
tall_grass          0.15   # clearings
fern                0.12   # moist shaded areas
bush                0.14   # understory shrubs throughout
moss_carpet         0.08   # at tree bases
lily_of_the_valley  0.05   # shade-loving forest flower
azalea              0.04   # understory shrub
peony               0.03   # large flowering shrub in clearings
rose_bush           0.02   # woodland edge
flowering_azalea    0.02   # rarer flowering variant
dandelion           0.03   # light gaps
poppy               0.02   # forest edge
firefly_bush        0.005  # exceedingly rare glow accent
```

### BIRCH_FOREST
Character: Light airy canopy. More bushes and flowers than dense forests. Dandelion meadows. Short grass for texture.
```
short_grass         0.38   # open canopy = lots of grass texture
tall_grass          0.22   # taller tussocks
leaf_litter         0.18   # birch leaf scatter (canopy-boosted)
bush                0.16   # more bushes in light forest
dandelion           0.08   # signature birch forest flower
fern                0.08   # north-facing shade (lower than bush)
lily_of_the_valley  0.04   # white clusters under trees
oxeye_daisy         0.04   # scattered white
azure_bluet         0.03   # delicate blue
azalea              0.04   # occasional shrub
lilac               0.02   # clearing accent
firefly_bush        0.005  # exceedingly rare
```

### MIXED_FOREST
Character: Moderate diversity. Leaf litter + fern + bushes. Balanced.
```
leaf_litter         0.22   # mixed deciduous/conifer litter (canopy-boosted)
short_grass         0.25   # base cover
tall_grass          0.15   # moderate
fern                0.14   # common in shade
bush                0.14   # understory bushes
large_fern          0.06   # deep shade patches
moss_carpet         0.06   # shaded areas
sweet_berry_bush    0.03   # woodland edge, rare
poppy               0.03   # scattered color
dandelion           0.02   # light gaps
azalea              0.03   # occasional shrub
firefly_bush        0.005  # exceedingly rare
```

### TEMPERATE_RAINFOREST
Character: Lush, dense, dripping. Fern dominates (realistic — this IS the fern kingdom). Moss everywhere. Bushes under canopy.
```
fern                0.40   # dominant — wet understory king
large_fern          0.25   # dense patches
moss_carpet         0.22   # thick moss on everything
leaf_litter         0.18   # decomposing litter (canopy-boosted)
bush                0.10   # understory shrubs
tall_grass          0.08   # rare clearings
short_grass         0.04   # sparse under heavy canopy
hanging_roots       0.05   # under rooted dirt overhangs
azalea              0.04   # shade shrub
firefly_bush        0.008  # exceedingly rare glow
```

---

## COASTAL / HEATH

### COASTAL_HEATH
Character: Wind-beaten, low scrub. Lots of short grass for texture on a sparse landscape.
```
short_grass         0.25   # bumped up — texture on sparse terrain
short_dry_grass     0.10   # salt-dried patches
tall_grass          0.08   # sheltered hollows only
bush                0.10   # wind-shaped low bush
dead_bush           0.05   # salt-burnt scrub
cornflower          0.02   # rare coastal wildflower
```

### EASTERN_TEMPERATE_COAST
Character: Rocky coast transitioning to grassland. Short grass dominant for texture.
```
short_grass         0.25   # bumped — sparse landscape texture
tall_grass          0.10   # sheltered areas
bush                0.09   # coastal scrub
short_dry_grass     0.06   # dry exposed patches
dead_bush           0.04   # exposed rock areas
azure_bluet         0.02   # coastal wildflower
```

### LUSH_RAINFOREST_COAST
Character: Extremely dense tropical cover. Ferns dominate. Vines on trees.
```
fern                0.42   # dominant tropical fern
large_fern          0.30   # dense double-height
tall_grass          0.18   # tropical grasses
moss_carpet         0.16   # thick ground moss
leaf_litter         0.15   # tropical litter (canopy-boosted)
bush                0.10   # dense understory
short_grass         0.06   # understory gaps
azalea              0.04   # flowering shrub
flowering_azalea    0.02   # rarer variant
vines               0.08   # tree trunk climbing
hanging_roots       0.04   # under overhangs
firefly_bush        0.01   # rare glow
```

### RAINFOREST_COAST
Character: Tropical but slightly drier. Ferns + grass + bushes.
```
fern                0.35   # strong fern presence
large_fern          0.20   # common
tall_grass          0.14   # tropical grass
bush                0.10   # understory
short_grass         0.08   # gaps and edges
moss_carpet         0.10   # moderate moss
leaf_litter         0.12   # tropical floor litter
vines               0.05   # tree climber
firefly_bush        0.005  # exceedingly rare
```

---

## DRY / ARID BIOMES

### DRY_OAK_SAVANNA
Character: Sparse dry grass, scattered dead bushes. African savanna feel.
```
short_dry_grass     0.30   # dominant dry savanna
tall_dry_grass      0.18   # taller dry tussocks
short_grass         0.10   # greener patches near moisture
bush                0.06   # scattered scrub
dead_bush           0.05   # dry patches
```

### CONTINENTAL_STEPPE
Character: Vast grassland. Tall grass dominant. Sunflower patches. Short grass for texture.
```
tall_grass          0.38   # dominant steppe grass
short_grass         0.30   # lots of short grass texture
tall_dry_grass      0.10   # dry season mix
short_dry_grass     0.08   # dry patches
bush                0.08   # scattered scrub
sunflower           0.04   # steppe wildflower patches
dead_bush           0.03   # drought patches
cornflower          0.02   # blue accent in moist spots
```

### DRY_PINE_BARRENS
Character: Sandy floor, sparse. Dry needles, occasional fern.
```
short_dry_grass     0.15   # dry pine floor
leaf_litter         0.10   # pine needle litter
dead_bush           0.07   # dry understory
short_grass         0.08   # sparse
bush                0.06   # occasional scrub
fern                0.05   # sheltered shady spots
resin_clump         0.02   # pine resin, very rare
```

### SCRUBBY_HEATHLAND
Character: Low scrub, heather-like. Short grass dominant for landscape texture.
```
short_grass         0.25   # bumped — landscape texture
short_dry_grass     0.12   # dry exposed areas
bush                0.12   # heath bushes — signature
tall_grass          0.10   # sheltered spots
dead_bush           0.06   # heath scrub
azure_bluet         0.03   # heather substitute
```

### SAND_DUNE_DESERT
Character: Nearly barren. Extremely rare survivors.
```
dead_bush           0.03   # sparse desert survivor
short_dry_grass     0.02   # rare sheltered tufts
cactus              0.005  # extremely rare desert column
```

### DESERT_STEPPE_TRANSITION
Character: Arid with dry grasses where moisture collects.
```
short_dry_grass     0.12   # dominant dry grass
tall_dry_grass      0.05   # drainage channels
dead_bush           0.05   # dry areas
short_grass         0.04   # moisture pockets only
bush                0.03   # rare scrub
cactus              0.005  # extremely rare
```

### SEMI_ARID_SHRUBLAND
Character: Scrubby. Dry grass and bushes dominate.
```
short_dry_grass     0.15   # dominant dry vegetation
tall_dry_grass      0.05   # drainage paths
dead_bush           0.08   # dry scrub
bush                0.08   # scattered shrubs
short_grass         0.06   # moisture channels
tall_grass          0.03   # rare moist spots
```

### DRY_WOODLAND_MAQUIS
Character: Mediterranean scrub. Grass, bushes, and wildflowers.
```
short_grass         0.20   # Mediterranean grass
short_dry_grass     0.14   # south-facing dry slopes
bush                0.12   # maquis scrub — signature
tall_grass          0.10   # taller areas
leaf_litter         0.06   # under oak canopy
dead_bush           0.04   # dry rocky patches
allium              0.03   # Mediterranean wildflower
poppy               0.04   # classic red poppy
torchflower         0.02   # warm-climate accent
```

### KARST_BARRENS
Character: Rocky, almost barren. Hardy crevice plants only.
```
dead_bush           0.04   # crevice survivors
short_dry_grass     0.03   # soil pocket tufts
bush                0.02   # rare crevice shrub
short_grass         0.02   # rare
```

---

## WETLAND / RIPARIAN

### RIPARIAN_WOODLAND
Character: Lush waterside. Tall grass, reeds, ferns, bushes. Dense.
```
tall_grass          0.38   # dominant riverbank grass
short_grass         0.22   # shorter layer
sugar_cane          0.15   # reeds at waterline
fern                0.14   # moist shaded spots
bush                0.10   # waterside bush
large_fern          0.08   # deep shade near water
moss_carpet         0.10   # wet ground moss
leaf_litter         0.08   # riparian litter
blue_orchid         0.04   # moisture-loving flower
azalea              0.03   # damp shrub
```

### FRESHWATER_FEN
Character: Wetland. Dense grass, reeds, saturated ground.
```
tall_grass          0.38   # dominant fen grass
short_grass         0.28   # dense lower layer
sugar_cane          0.20   # abundant reeds
fern                0.08   # raised hummocks
moss_carpet         0.12   # waterlogged moss
bush                0.08   # fen edge scrub
blue_orchid         0.05   # wet meadow flower
lilac               0.02   # fen edge shrub
```

### MANGROVE_COAST
Character: Mud flats, sparse vegetation. Reeds near water.
```
tall_grass          0.10   # sparse on mud
sugar_cane          0.08   # near water edge
short_grass         0.05   # raised spots
bush                0.04   # rare
hanging_roots       0.05   # under mangrove canopy
moss_carpet         0.03   # wet mud patches
```

### TIDAL_JUNGLE_FRINGE
Character: Dense tropical wetland. Ferns, grass, reeds, bushes.
```
tall_grass          0.28   # tropical wetland grass
fern                0.22   # tropical fern
large_fern          0.12   # dense patches
sugar_cane          0.10   # reeds along water
bush                0.08   # wetland shrub
short_grass         0.08   # understory
moss_carpet         0.08   # wet ground
leaf_litter         0.06   # tropical debris
vines               0.05   # climbing vegetation
firefly_bush        0.01   # rare glow accent
```

---

## AQUATIC VEGETATION (new placement pass)

### Shallow Ocean (depth 1-4 blocks below sea level)
```
seagrass            0.25
lily_pad            0.06   # surface only at depth=1
```

### Mid Ocean (depth 5-12 blocks)
```
tall_seagrass       0.15
seagrass            0.08
```

### Deep Ocean (depth 13+ blocks)
```
kelp                0.06
```

### River/Stream Shallows (depth 1-3)
```
seagrass            0.18
lily_pad            0.10   # calm water surface
```

### Lake Surface (hydro_lake, depth 1-2)
```
lily_pad            0.20
seagrass            0.15
```

---

## SPECIAL PLACEMENT NOTES

1. **Vines**: Vertical surface placement (tree trunks, cliff faces). Tropical biomes.
2. **Hanging roots**: Under rooted_dirt/dirt where air below. Rainforest, mangrove.
3. **Cactus**: Column 1-3 blocks. Sand below, no adjacent blocks. EXTREMELY rare.
4. **Double-tall plants**: Bottom at surface_y+1, top at surface_y+2. Needs 2 air blocks.
5. **firefly_bush**: 0.005-0.01 max. Shiny super rare accent only.
6. **resin_clump**: 0.01-0.02 max. Present but subtle.
7. **sweet_berry_bush**: Block state `sweet_berry_bush[age=3]`. Low density (0.03 max). Deferred if complex.
8. **leaf_litter**: Canopy-boosted — density multiplied by canopy_proxy so it naturally clusters where trees grow dense.
9. **pale_moss_carpet**: NOT in tundra biomes (prevents snow accumulation).
10. **Mangrove roots**: Complex block networks — deferred to schematic enhancement pass.
