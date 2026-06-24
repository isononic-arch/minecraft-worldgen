# island_geo_data — real land-cover / vegetation rasters per island

Drop real-world land-cover data here to override the procedural altitude-band
biomes with real biomes. The island painter (`islands/island_painter.py`) reads
from this folder; the bake (`render_islands.py`) honors the painted/raster layer
and falls back to altitude bands only where unpainted.

## Layout (one folder per island, keyed by the DEM filename stem fragment)
    island_geo_data/<key>/landcover.tif      # or .png — categorical or RGB raster
    island_geo_data/<key>/classmap.json      # OPTIONAL: {raster_class_or_hex: ZONE_NAME}
    island_geo_data/<key>.zip                # OR a zip containing the above

`<key>` = the BANDS key (the lat_lon fragment of the DEM filename), e.g.
`17_288` for New Vincentia (St Kitts/Nevis/Statia).

## Raster expectations
- Same geographic footprint/orientation as the island DEM (so it aligns after the
  same flip/rotate the bake applies). If it's a different crop, align it first.
- Categorical: integer class codes -> mapped to Vandir zones via classmap.json
  (or the default ESA-WorldCover-ish LUT in import_island_raster.py).
- RGB land-cover map: dominant-color -> zone via classmap.json hex keys.
- No-data / ocean / unmapped classes -> left 0 (the bake uses altitude bands there).

## Zones (paint targets) — see core.biome_assignment.OVERRIDE_BIOME_MAP
    0 
   10 COASTAL_HEATH
   20 TEMPERATE_RAINFOREST
   30 BOREAL_TAIGA
   35 SNOWY_BOREAL_TAIGA
   40 BOREAL_ALPINE
   50 ARCTIC_TUNDRA
   55 FROZEN_FLATS
   60 TEMPERATE_DECIDUOUS
   70 RAINFOREST_COAST
   80 RIPARIAN_WOODLAND
   90 DRY_OAK_SAVANNA
  100 KARST_BARRENS
  110 BIRCH_FOREST
  115 EASTERN_TEMPERATE_COAST
  120 MIXED_FOREST
  130 CONTINENTAL_STEPPE
  140 DRY_PINE_BARRENS
  150 SCRUBBY_HEATHLAND
  160 LUSH_RAINFOREST_COAST
  170 SAND_DUNE_DESERT
  190 DESERT_STEPPE_TRANSITION
  200 SEMI_ARID_SHRUBLAND
  210 DRY_WOODLAND_MAQUIS
  220 TIDAL_JUNGLE_FRINGE
  230 MANGROVE_COAST
  240 FRESHWATER_FEN
