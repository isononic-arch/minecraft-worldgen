# Vandir — Architecture Vision & Strategic Roadmap
*Originally written Session 14, 2026-03-19. Substantially revised Session 15, 2026-03-24. Tool E section updated Session 18, 2026-03-27. River/hydrology sections updated Session 28, 2026-03-31.*
*Read this before making any structural decision about the pipeline.*
*This document describes the destination. CLAUDE.md describes where we are now.*

---

## The Core Philosophy

This is not a procedural world generator. It is a **terrain translation pipeline** — Gaea
produces what is functionally satellite elevation data of a fictional planet, and our job is to
translate that planet faithfully into a navigable Minecraft world at 1:1 scale. Every
architectural decision should flow from this framing.

The world is 50,000 × 50,000 blocks, 512 vertical blocks of range (Y −64 to 448). At natural
walking speed a player takes ~3 hours to cross it. A mountain range is 5–10km wide. A river
system drains hundreds of square kilometres. At this scale, nothing can be solved locally
per-tile — rivers, climate zones, geological strata, and narrative structure are all continental
features that span thousands of tiles. The pipeline must think globally and render locally.

**The standard for quality**: the world should feel like the player discovered a real place that
existed before they arrived, not like terrain that was generated for them. Every biome, every
cliff face, every river bend should have the internal logic of geology and climate behind it,
even if the player never consciously notices. Reference benchmark: Dobrze/Greenfield/Orgrimmar
recreation-level fidelity — blocks chosen as an artist would choose them, not as a programmer
would default them.

**The iteration model**: the 50k MCA export is a one-click final output, not the iteration
surface. All creative decisions — biome tuning, surface palettes, geological detail, structure
placement — are validated in the integrated voxel viewer first. The integrated tool IS the
pipeline UI. The full render happens once, when everything is right.

---

## Part I — The Ideal Pipeline Architecture

### Stage 0 — Geodata Registration (run once, permanent)

All source files from Gaea (height.tif, flow.tif, erosion.tif, etc.) are registered in a
`geodata_manifest.json` that stores a single affine transform matrix describing the relationship
between Gaea's coordinate space and Minecraft's block coordinate space. This includes flip axes,
scale factor, and X/Z offset.

**This is the fix for the alignment problem.** Currently flip constants and offsets are scattered
across three files. In the ideal state, the manifest is the one source of truth, and every
pipeline read passes through a `GeoTransform` class that applies it transparently. The aligner
tool writes to the manifest. Nothing else needs to know about coordinate transforms.

```json
{
  "transform": [[sx, 0, tx], [0, sz, tz]],
  "flip_x": true,
  "flip_z": true,
  "source_crs": "gaea_pixel",
  "target_crs": "minecraft_block"
}
```

### Stage 1 — Derived Mask Generation (pre-baked, cached)

All masks derived from the height field are computed once across the full 50k extent and stored
as float32 GeoTIFFs. These never need to be recomputed unless height.tif changes.

Required derived masks (beyond what currently exists):
- `slope.tif` — gradient magnitude (partially exists)
- `aspect.tif` — slope direction in degrees (0=N, 90=E, 180=S, 270=W) — drives snow line
  variation, vegetation density, precipitation shadow
- `curvature.tif` — convexity/concavity — ridgelines (convex) vs. valley floors (concave),
  scree placement, talus cone detection
- `ridgeline.tif` — binary mask of topographic ridges (local maxima along principal curvature)
- `valley_floor.tif` — binary mask of valley floors (local minima with converging flow)
- `shore_distance.tif` — proper 2D Euclidean distance field from coastline (not a threshold)
- `river_network.tif` + `river_network.geojson` — extracted from flow.tif as a proper vector
  network with Strahler stream order, width, depth, and meander curvature per segment
- `wind_exposure.tif` — windward/leeward index derived from aspect + tradewind direction
  (see Climate Model below)

**Mask Generation Standard (Session 41)**

All `rebuild_*.py` scripts follow this pattern:
1. Read masks at 1:8 (6250×6250) via `read_ds(name, Resampling.average)`
2. Compute derived fields (slope/aspect/concavity/etc.) — **use `np.gradient(sy)/SCALE`** for
   correct block-space slope, NOT raw gradient (which inflates slopes ~8x)
3. Compose mask via **weighted SUM** of physical signals (Session 39 lesson — multiplicative
   composition crushes scores). Multiplicative GATES (slope class, biome) are applied as
   final filters.
4. Smooth biome-driven values (treelines etc.) with `gaussian_filter(sigma=15)` to avoid
   visible biome-shaped seams
5. Bilinear upscale to 50k via `write_upscaled(interpolation="bilinear")`
6. Register in `core/tile_streamer.py` MASK_NAMES
7. Wire through `core/eco_gradients.py` (gap_mask) and/or `core/surface_decorator.py`

Reference implementations:
- `rebuild_sand_dunes.py` — weighted-sum desert mask with corrected slope math
- `rebuild_rock_exposure.py` — three-mask output (alpine + tight rock + snow)

### Stage 1a — Global Climate Model

**Tradewinds blow west to east** (this is a fixed world constant, not configurable).

Derived from this single rule, before any per-tile work:

- `wind_exposure.tif`: each pixel gets a windward score (0=full leeward, 1=full windward)
  based on aspect relative to the prevailing W→E wind direction. West-facing slopes score 1,
  east-facing slopes score 0, interpolated by cosine of angle difference.
- **Precipitation shadow**: east-facing slopes behind mountain ranges are drier. The moisture
  estimate for biome assignment uses wind_exposure as a multiplier.
- **Snow line modulation**: the base snow line (Y 320) is lowered by up to 35 blocks on
  north-facing windward slopes (more accumulation, less ablation) and raised by up to 40 blocks
  on south-facing leeward slopes. Specifically:
  - North-facing (aspect 315–45°): snow_line_y − 30 + wind_exposure × (−20)
  - South-facing (aspect 135–225°): snow_line_y + 30 + (1 − wind_exposure) × 20
  - East/west neutral faces: interpolate
- **Vegetation density gradient**: windward slopes have denser ground cover (multiply
  decoration_density by wind_exposure × 0.4 + 0.8). Leeward slopes are sparser and shift
  toward drought-tolerant species in the decoration palette.

### Stage 2 — Global Biome Assignment (vectorized, world-wide, cached)

Biome assignment runs as a single vectorized pass across the entire 50k extent and produces
`biome_assignment.tif` — the ground truth biome map. Not recomputed per tile at render time.

The assignment logic uses (in priority order):
1. Override layer — explicit artist control, highest priority
2. Climate model: height + aspect + wind_exposure + shore_distance → temperature/moisture
3. Hydrological zones: river_network proximity → riparian biomes
4. Slope character: ridgeline/valley/planar → vegetation modifiers
5. Noise patches: fine-grain variation within broad zones

The biome map is tunable from the GUI (Biome Studio, Tool D). No JSON editing in production.

### Stage 2a — Geological Character Map

Each pixel in the world is assigned a geological domain based on biome + height band. The
geological domain determines what rock type is exposed in cliff faces, what the subsurface
strata are, and what the talus/scree material is. This is a global TIF (`geo_domain.tif`),
consistent with the biome block palettes already defined:

| Geological Domain | Primary Rock | Secondary Rock | Biomes |
|---|---|---|---|
| Volcanic Coastal | andesite | stone | COASTAL_HEATH, EASTERN_TEMPERATE_COAST, SCRUBBY_HEATHLAND |
| Crystalline Northern | diorite | stone | BOREAL_TAIGA, SNOWY_BOREAL_TAIGA, BIRCH_FOREST |
| Calcareous Alpine | calcite | stone | ALPINE_MEADOW, KARST_BARRENS |
| Acid Granitic | granite | stone | DRY_PINE_BARRENS, DRY_WOODLAND_MAQUIS |
| Volcanic Arctic | andesite | tuff | ARCTIC_TUNDRA, FROZEN_FLATS |
| Siliciclastic Arid | sandstone | smooth_sandstone | SAND_DUNE_DESERT, DESERT_STEPPE_TRANSITION |
| Mixed Sedimentary | cobblestone | stone | MIXED_FOREST, TEMPERATE_DECIDUOUS, CONTINENTAL_STEPPE |
| Organic/Alluvial | mud | clay | RIPARIAN_WOODLAND, FRESHWATER_FEN, TIDAL_JUNGLE_FRINGE |
| Tropical Weathered | mossy_cobblestone | stone | TEMPERATE_RAINFOREST, RAINFOREST_COAST, LUSH_RAINFOREST_COAST |

**Note on CONTINENTAL_STEPPE**: currently uses andesite (coastal volcanic rock — incorrect for
an inland steppe). Switch to granite/cobblestone (mixed sedimentary, inland).
**Note on DRY_OAK_SAVANNA**: currently lacks stone character. Add terracotta + red_sand variants
at high erosion — savanna laterite exposure.

### Stage 3 — Global Decoration Planning (pre-solved, cached)

Everything that spans tile boundaries is solved globally before any chunk renders.

#### 3a — River Carving Plan

`river_carve_plan.tif`: for every pixel, the carve depth below natural terrain surface. Derived
from river_network.geojson with the following rules:
- Width: 3 blocks at Strahler order 1 → 20 blocks at order 5+
- Depth: 1 block at order 1 → 4 blocks at order 5+ (max_carve_depth=4). Reduced from original
  2→10 to avoid overly deep trenches at Minecraft scale.
- **Edge smoothing**: Gaussian blur (sigma=4.5, threshold 0.12) on centerline mask before EDT
  eliminates 8x8 NEAREST upscale staircases, producing organic river curves.
- **Meanders**: river_carver_v2 includes meander path generation with smooth propagation.
- Banks: inner bend (point bar) receives sand/gravel scatter; outer bend receives undercut
  (one block removed at bank foot). Bank material is 1–2 blocks wide — 1 block = 1 metre.
- **Bank scatter composition**: fine-scale opensimplex noise (scale=8, octaves=2) drives
  per-pixel material selection: 55% mud, 25% coarse_dirt, 12% clay, 8% dirt. Replaces old
  coarse noise (scale=60) which produced monotone patches. Sugar cane removed from banks
  (caused entity lag from despawning).
- **Connectivity**: `enforce_connectivity()` extends rivers that are order >= 2, OR any order
  within 50px (400 blocks) of coastline. Hybrid approach balances coverage (~111k river pixels).
- **Water fill**: `build_column_array` accepts `river_water_y` parameter. For above-sea-level
  carved pixels, water fills from carved surface up to `pre_carve_y - 1`. Seagrass/tall_seagrass
  placed on riverbed at ~25% density.
- Tributaries join at correct Y — no waterfalls at confluences unless deliberate.
- Rivers reach ocean at or below Y 63.

#### 3b — Lake Placement Plan

`lake_plan.tif` + `lake_plan.json`: lakes are placed globally, sparse, and always tied to the
river network. Every lake has an inflow river and (if not terminal) an outflow.

**Selection criteria** (from flow.tif + height.tif):
- Flow accumulation basin meeting a minimum catchment area threshold (tunable, default ~0.9
  of max flow)
- Basin must have a topographic closure (height saddle higher than basin floor on all sides)
- Not within 200 blocks of coastline (coastal basins become tidal features, not lakes)

**Lake types by scale** (all sparse — this is a naturally arid/highland planet):
- Mountain tarns: 2–12 block radius, above Y 200, in glacial cirques or alpine hollows.
  Floor: bare stone/gravel. Banks: angular rock, no beach.
- Valley lakes: 20–80 block radius, Y 80–200. Floor: gravel → silt toward centre.
  Banks: gravel/coarse_dirt in rocky biomes, mud/grass in forest biomes, gravel/sand in open.
  1–2 block-wide bank material, scattered transition (not banded).
- Lowland lakes: 50–200 block radius, Y 63–100. Floor: clay/silt (represented as mud).
  Banks: mud + clay scatter in wetland biomes, sand + gravel in open biomes. Often with
  adjacent FRESHWATER_FEN or RIPARIAN_WOODLAND biome halo.
- Ox-bow lakes: detached from meander, elongated, shallow (max depth 4 blocks). Mud floor.
  No beach — overgrown margins of mud, tall_grass, sugar_cane.

**Water surface**: flat per lake at a defined Y. Never terrain-shaped water. Sealed on all sides.

**Bank variation approach**: banks never use a single block type across more than 3 consecutive
surface pixels. Use weighted random scatter: 50% primary material, 30% secondary, 20% tertiary,
chosen per-pixel using decorating noise seed.

#### 3c — River Delta Plan

Deltas form where Strahler order ≥ 3 rivers reach within 150 blocks of the ocean at gradient ≤
0.002 (very shallow slope). Tight execution plan:

1. **Identify delta apex**: the point where the river first meets the tidal zone (shore_distance
   < 150 blocks and surface_y ≤ 65).
2. **Fan geometry**: from the apex, generate 2 (order 3–4) or 3 (order 5+) distributary
   channels using bearing offsets of ±15–25° from the main channel azimuth.
3. **Each distributary**: progressively shallower (depth −0.3 blocks per 10 blocks from apex)
   and wider (+0.5 blocks per 10 blocks). Channels are carved into the delta platform, not the
   original terrain.
4. **Inter-distributary islands**: raised sediment bodies between channels. Surface Y: 63–65
   (just at or above sea level). Material: mud base with clay + gravel scatter (~20% each).
   Vegetation: tall_grass, sugar_cane fringe on the seaward edge.
5. **Tidal flat**: seaward of all channels, Y 61–63, mud surface with clay scatter. No beach.
   Transitions directly to ocean floor.
6. **Width constraint**: total delta width at the coastline = river width at apex × 4.
7. **Validation check**: all distributary channels must connect to ocean water. Any channel that
   dead-ends gets merged with its neighbour.

#### 3d — Structure Placement Plan

`structure_plan.json`: every schematic footprint placed globally before any tile renders.
- World block coordinates (not tile-relative)
- Biome compatibility verified against biome_assignment.tif
- Terrain flatness verified against slope.tif (footprint slope < threshold)
- No overlap with other structures, rivers, or lakes
- Narrative category: geological feature / ecological landmark / water feature

No roads. No man-made structures. The world has never been inhabited.

### Stage 4 — Chunk Generation (embarrassingly parallel, checkpointed)

Each tile receives its window of all mask TIFFs, its slice of biome_assignment.tif,
river_carve_plan.tif, lake_plan.tif, and its structure placement list. Fully self-contained.

**Checkpointing**: every successfully rendered tile is logged to `render_manifest.json` with a
content hash of its input parameters. Re-running skips tiles whose inputs haven't changed.
Changing a surface palette only re-renders tiles containing that biome. Iteration is cheap.

**Output summary**: each tile writes biome coverage, min/max Y, structure count to the manifest.
The world map reads this for render-status display.

### Stage 5 — Post-Render Validation

- **Boundary seam check**: every tile-edge pair — block types must match at boundaries
- **River continuity**: every river segment traced across tile boundaries, water connects
- **Lake integrity**: every lake sealed, water surface flat, no drainage
- **Structure integrity**: every planned placement has blocks present
- **Height cap**: no column exceeds Y 448 or goes below Y −64
- **Biome tag**: MC biome tags align with override assignment

---

## Part II — The Integrated Tool Suite

**One application. Not a collection of scripts.**

### Design Principles

The tool is designed as a professional creative application, not a developer utility. Design
references: Mapbox Studio (clean map-first interface), DaVinci Resolve (dark professional
theme, organised panels, non-destructive workflow), Figma (precision inputs with immediate
visual feedback, minimal chrome). Every parameter is reachable within two clicks of the main
view. No terminal. No JSON editing. No file paths typed by hand.

**Visual language**:
- Dark background (#1a1a1a base), warm neutral panels (#252525), accent colour for interactive
  elements (single consistent hue — deep amber or slate blue, not a rainbow)
- Typography: monospace for numerical values, clean sans-serif for labels
- Status always visible: render progress, last-modified tile, current parameter state
- Histogram presence everywhere a threshold exists — seeing the data distribution is not
  optional, it is the core UX

### Tool A — World Map (primary view)

A zoomable, pannable view of the full 50,000 × 50,000 world with toggleable overlay layers:

- **Height** — terrain colormap
- **Biome** — false-colour biome assignment
- **Hydrology** — flow accumulation + river network vectors + lake outlines
- **Geology** — geological domain map (rock type per region)
- **Wind exposure** — windward/leeward gradient
- **Climate** — precipitation and temperature estimates
- **Structures** — schematic footprint overlays by category
- **Render status** — tile grid: grey=unrendered, amber=outdated, green=current, red=failed
- **Annotations** — artist notes layer (see Tool G)

**Interaction**:
- Click any tile → opens Tile Inspector (Tool C)
- Right-click → "Render this tile to voxel", "Queue for MCA export", "Annotate"
- Drag to select a region → "Render region", "Preview biome distribution"
- Scroll wheel zoom with smooth LOD transition (overview → tile-level → pixel-level)

### Tool B — Live Config Panel

Every threshold in thresholds.json exposed as a labelled slider/input with:
- Current value and its distribution histogram behind it
- A threshold marker showing where it cuts the distribution
- Estimated % of world affected
- "Affected tiles" count that updates within 500ms of any change

Changing a value → affected tiles flash amber on world map. "Preview" re-renders visible
tiles in the voxel viewer. "Commit" saves config and queues a background re-render of all
affected tiles. No JSON editing ever.

### Tool C — Tile Inspector

Click any tile to open:
- Four render modes: height / biome / slope / surface_block
- Cross-section at any row (click to move) — shows full Y column profile
- **Block column detail**: hover any pixel → full block stack bedrock to surface, with block
  name, Y value, and which pipeline step placed it
- Hover status bar: biome, MC Y, tile coords, geological domain, wind exposure index
- **Render to voxel** button — updates Tool E view for this tile
- **Export to MCA** button — writes .mca and hot-swaps into running Minecraft via file
  replacement (MC must be paused/in menu for file swap to be safe)

### Tool D — Biome Studio

A 2D scatterplot of every pixel in the current visible region:
- X axis: normalised height (0–1, sea level marked)
- Y axis: slope magnitude
- Z-colour: wind exposure index (windward warm, leeward cool)
- Point colour: current assigned biome
- Draggable threshold lines — move a line, points re-colour in real time
- Lasso tool: select a cluster, assign a biome override manually
- "Apply to region" → writes override to override.tif for the selected area

This is how biome boundaries are tuned correctly — by seeing the data, not guessing numbers.

### Tool E — 3D Voxel Preview (centrepiece of the iteration loop)

**This is the primary feedback surface. The full 50k render is the final output, not the
iteration surface.**

**Current implementation (Session 17–18)**: numpy hillshade renderer. `FastIsoLoader` reads
height.tif via rasterio Window on tile select — instant preview without running the pipeline.
`render_hillshade()` computes gradients in actual MC block units (`h_f32 * Y_RANGE * h_scale`),
builds surface normals, and dots against a configurable sun direction. `TileCanvas` (QWidget +
paintEvent) displays the result with pan/zoom. Sun azimuth and vertical exaggeration are
exposed as sliders. "Generate Colors" pipeline button runs the full column generator and
enriches the hillshade with actual block surface colors. This is a stable working tool.

**Why WebGL was abandoned**: `QWebEngineView` proved too fragile — canvas element was being
squeezed to ~547×134px by the Qt layout engine, the `loadFinished` signal fired before the
connection was established (signal missed forever), and encoding terrain data had an O(65536)
Python loop causing UI freezes. These issues stacked and were not worth solving; numpy hillshade
gives adequate feedback for all current iteration work.

**Ideal destination (still valid)**: WebGL fly-through with WASD + mouse look, configurable fly
speed, keyboard shortcuts to snap to cardinal views. Instanced mesh rendering — each unique
block type is one draw call. Target < 8 seconds per tile. This remains the Phase 5 quality goal
but is not blocking current work.

**Camera (current)**: pan (drag) and zoom (scroll wheel) on the hillshade image. No fly-through.

**Rendering features**:
- Per-block colour palette mapped from MC block names (tunable colour table, not MC textures)
- Directional lighting: simulated sun angle with configurable azimuth (matches MC time of day)
- Ambient occlusion baked at voxel boundaries — critical for reading cliff faces and overhangs
- Water: semi-transparent blue tint, flat surface at sea level
- Snow: white tint on top faces above snow line
- **Cross-section slice**: cut the volume at any Y plane with a slider — exposes underground
  strata, river carve depth, lake floors, subsurface geology
- **Subsurface X/Z slice**: vertical wall cut through the volume at any X or Z, revealing the
  full geological column

**LOD system**:
- Full voxel resolution within 64 blocks of camera
- 4×4 column average (single tall block per column) at 64–256 blocks
- Heightmap mesh with colour at >256 blocks
- Transition is seamless — no pop-in visible at camera speeds < 20 blocks/second

**Side-by-side comparison**: split the view into two independent panels, each showing a
different parameter state. Linked camera movement optional. Essential for before/after threshold
tuning.

**Multi-tile view**: render a 3×3 or 5×5 tile cluster simultaneously at reduced LOD to
evaluate tile boundary continuity and regional landscape character.

**Render queue integration**: "Render visible tiles to voxel" button in the toolbar queues all
tiles in the current world map viewport. Background threads generate volumes; tiles appear in
the voxel view as they complete.

### Tool F — Structure Placer

Map overlay mode:
- All schematic footprints shown as coloured outlines on the world map
- Click → schematic name, biome requirement, Y anchor, dimensions
- Drag to reposition (snaps to flat terrain automatically via slope.tif)
- Right-click → Remove, Duplicate, Change schematic
- Filter by category (geological / ecological / water feature)
- "Auto-place" for a category: finds all valid locations at configured density

Structure placement is not a config file. It is a visual, spatial design tool.

### Tool G — Annotation Layer

Persistent artist notes on the world map:
- Click anywhere → place a note marker with text, severity (info/warning/fix-needed), and
  an optional screenshot attachment
- Notes stored in `annotations.json`
- Filter by severity — "show only fix-needed" to see your backlog
- Notes appear in Tile Inspector when that tile is selected
- Resolve a note → it becomes a completed annotation (still visible, strikethrough style)

---

## Part III — World Quality Standards

### Geological Coherence

**Cliff faces and exposed strata**:
Cliff faces steeper than 55° expose subsurface geology. Each geological domain has a strata
sequence (3–5 layers, 4–14 blocks thick each, with ±noise on thickness):
- Volcanic Coastal: andesite base → stone mid → andesite cap, occasional cobblestone band
- Crystalline Northern: diorite base → stone mid → gravel weathering band at top
- Calcareous Alpine: stone base → calcite mid → calcite cap, white band visible from distance
- Acid Granitic: granite base → stone mid → gravel surface layer
- Volcanic Arctic: tuff base → andesite mid → gravel frost-shatter cap

Strata bands follow terrain curvature (tilt with slope direction, not horizontal). Band
thickness varies with `band_noise_scale_xz` noise. No horizontal banding on organic/alluvial
biomes (those cliff faces are pure stone — they're uncommon there).

**Talus cones**: at cliff bases where curvature.tif shows concavity below a steep face,
cobblestone/gravel scatter in a fan pattern. Width scales with cliff height. 40% primary
rock type, 40% stone, 20% gravel — broken appearance, not a solid fill.

**Bedrock exposure at peaks**: above Y 340, soil mantle disappears. Stone/primary rock
exposed directly. No grass, no gravel surface — raw rock. Snow accumulation on raw rock
surfaces begins at Y 300 and reaches 100% by Y 380. Snow line varies by aspect (see Stage 1a).

**Frost-shattered ridgelines**: ridgeline.tif pixels above Y 300 → scattered individual stone
blocks placed 1–2 blocks above the surface. Density 15–30%. These represent frost-heaved
boulders and frost-shattered debris. Same rock type as geological domain.

### Hydrological Integrity

**Rivers** are the skeleton. They must:
- Be continuous source-to-sea with no breaks across tile boundaries
- Widen 3→20 blocks and deepen 1→4 blocks from headwater to tidal zone (max_carve_depth=4)
- Have smooth organic edges (Gaussian blur on centerline before EDT, not raw pixel staircases)
- Have banks 1–2 blocks wide using fine-scale opensimplex noise scatter (55% mud, 25% coarse_dirt, 12% clay, 8% dirt)
- Have correct geomorphology: meander paths, sandy point bars on inner bends, undercut outer bends
- Have proper water fill: above-sea-level rivers filled from carved bed to pre_carve_y - 1
- Have riverbed vegetation: seagrass/tall_seagrass at ~25% density on submerged blocks
- Meet tributaries at correct Y (no unintentional waterfalls at confluences)
- Reach ocean at Y ≤ 63
- No sugar cane on banks (despawns, causes entity lag)

**Lakes**: see Stage 3b. Sparse, varied, river-connected. Banks use scatter, never
monotone material. Biome-appropriate material, 1–3 blocks wide.

**Deltas**: see Stage 3c. Tight plan already specified.

### Biome Transition Quality

Blend zones between biomes, width proportional to ecological dissimilarity. Surface blocks
and ground cover density interpolate across the transition. The existing boundary jitter is
step one. The decoration system must respect blend-zone probability functions.

### Vertical Character by Elevation Band

| Y Range | Character | Key blocks |
|---|---|---|
| −64 to 20 | Ocean deep / cave floors | Stone, gravel, clay |
| 20 to 63 | Shallow ocean / lake floor / river bed | Sand, gravel, water |
| 63 to 100 | Coastal plain / river floodplain | Grass, sand, coarse_dirt |
| 100 to 200 | Lowland / highland / forest | Grass, podzol, dirt, moss |
| 200 to 320 | Alpine transition / montane | Stone, coarse_dirt, gravel, scree |
| 320 to 380 | High alpine / partial snow | Stone + primary rock, snow patches |
| 380 to 448 | Peak / full snowcap | Raw stone, full snow/ice, tuff |

Each band is immediately readable from a distance. Snow line varies by aspect (Stage 1a).

### Block Palette Quality Standards

Reference aesthetic: hyperrealistic Minecraft geography projects (Greenfield, Terra 1 to 1,
realistic terrain mods). Key rules that apply across all biomes:

1. **No monotone surfaces**: any surface material used for more than 3 consecutive blocks in
   any direction should have ≥20% scatter of adjacent compatible materials.
2. **Transitions are thin**: bank material, beach edges, scree fringes — 1–3 blocks wide.
   Real geography transitions in metres, not tens of metres.
3. **Stone types are geologically consistent within a region**: don't mix granite and calcite
   in the same cliff face. The geological domain map enforces this.
4. **Pure-sand fields ARE realistic for sand dune regions** (Session 41 update — earlier rule
   wrong). Real sand seas are uniform sand. The "no pure surfaces" rule applies to BIOME
   palettes (which would otherwise blob), not to gap-mask-driven specialized zones which
   correctly use pure blocks (sand_dune, snow_cap, etc.). Variation comes from the 8 gap
   types, not from biome palette noise layers.
5. **Mud is a transition material, not a base**: mud appears at waterlines, riverbanks, wetland
   margins. Never as a large surface fill.
6. **PHYSICAL REALISM LAYER pattern (Session 41 STANDARD)**: SURFACE BLOCK painting for
   geological features (rock, snow, sand, basalt cap rock, stratification) must use
   PHYSICAL signals (aspect, concavity, slope, flow, wind direction) as primary discriminators.
   Noise is ±10% edge jitter ONLY for these geological features — never the primary
   block-selection driver. Layer-by-layer composition with the "decisive" block (basalt cap
   rock, snow_block, etc.) as the LAST step. Reference: `core/surface_decorator.py:
   _apply_desert_rock_palette()` and `memory/feedback_physical_realism_layer.md`. This
   pattern eliminates the "noise blob mush" rejected multiple times by user.

   **Noise still has its place** for ORGANIC ground cover variation: forest floor mixes
   (podzol/grass/rooted_dirt patches), grass color mixing, leaf litter scatter, mushroom
   patches, mossy stone variation. The rule against noise is for HARD geological features
   (rock palettes, stratification, cliff faces) where physical processes drive the variation.
   Soft biological / organic mixing layers can use noise freely — they don't read as "blobs"
   because organic matter IS clustered and patchy in real life.

---

## Part IV — Ecological Narrative

The world has no human history. It has geological and ecological history instead.

**Geological features**: the world's pre-history is written in rock. A glacially carved U-valley
in the highlands. A collapsed volcanic caldera at the continental divide. A massive river gorge
cutting through limestone (karst) into the coastal plain. Sea stacks off a high-energy
coastline. These are not scripted — they emerge from the terrain data. The pipeline's job is
to ensure the block choices make them legible.

**Ecological succession**: not every surface is mature. Disturbed zones (high erosion.tif
values, steep slopes, river flood plains) show earlier successional stages — coarse_dirt and
gravel where forest would otherwise be, moss and coarse_dirt where grassland would be.

**Water as narrative**: the river network is the world's geography lesson. Following a river
from alpine tarn to ocean delta teaches the player the planet's topography. Lakes are rest
points. Deltas are destinations. The hydrological system must be navigable and coherent.

---

## Part V — The Development Workflow in Target State

1. Open the integrated tool. World map loads in 2 seconds from cache.
2. Spot an issue in the NE highlands — biome reads wrong. Click the region.
3. Tile Inspector opens. Hover reveals CONTINENTAL_STEPPE at Y 180 — should be ALPINE_MEADOW.
4. Switch to Biome Studio. The height/slope scatterplot shows the ALPINE_MEADOW threshold
   cutting too low. Drag the line up.
5. Affected tiles flash amber. Click "Preview affected" → 12 tiles re-render in the voxel
   viewer in < 30 seconds.
6. Looks right. "Export tile (52,34) to MCA" — done in 25 seconds.
7. Minecraft hot-swaps the chunks. Walk the terrain.
8. Confirmed. Commit the threshold. Background queue re-renders all 12 affected tiles.
9. While that runs, add an annotation: "check river carve depth in this valley."
10. Total time from "I want to try something" to "I've seen it in 3D": under 2 minutes.

---

## Part VI — Current State Gap Analysis

| Capability | Current State | Ideal State | Gap |
|---|---|---|---|
| Coordinate registration | Constants in 3 files | geodata_manifest.json + GeoTransform | Medium |
| Derived masks | height, flow, slope partial | Full suite + aspect, curvature, ridge, wind | Large |
| Climate model | None | Tradewind-driven precipitation/snow | Large |
| Biome assignment | Per-tile at render time | Pre-baked global TIF | Large |
| Geological domains | Implicit in palettes | Explicit global domain map | Medium |
| River system | Global hydro precompute + river_carver_v2 (meander, smooth edges, water fill, bank noise) | Global vector network + full carve plan | Medium |
| Lake system | threshold exists, unused | Global placement plan, all types | Large |
| River deltas | Not implemented | Tight global delta plan | Large |
| Structure placement | JSON index, load_index bug | Visual placer, global plan | Large |
| Tile boundary continuity | Not solved | Global decoration plan | Large |
| Subsurface geology | Surface-only | Strata bands on cliff faces, talus | Medium |
| Snow line | Fixed Y 320 | Aspect + wind exposure modulated | Medium |
| Pipeline checkpointing | None | Content-hash per tile | Medium |
| Voxel preview | Numpy hillshade (Session 17–18) | WebGL fly-through, < 8s per tile, LOD, A/B compare | Medium |
| Config editability | JSON files | Live GUI with histograms | Large |
| Biome tuning | Re-render to see | Biome Studio scatterplot, real-time | Large |
| In-game iteration | Full render cycle | Hot-swap single tile | Large |
| Block palette quality | Functional | Scatter rules, geologically consistent | Medium |
| Annotation system | None | Persistent artist notes on world map | Small |

---

## Part VII — Priority Order

### Phase 1 — Foundation (current)
Work through these before any full render. The voxel preview is the feedback loop.

1. **Fix load_index bug** — schematic placement must work
2. **Wire surface_palettes to surface_decorator** — blocks must reflect biome character
3. **Global river carve plan** — rivers must be continuous across tiles
4. **Geological surface detail** — strata, talus, bedrock exposure, frost peaks
5. **Tile boundary checker** — automated seam detection

### Phase 2 — Integrated Tool (parallel with Phase 1)
Build the tool as Phase 1 items land — each fix gets a UI surface immediately.

6. **World map + render status layer** — DONE (Session 17): 97×97 grid, LOD thumbnails, tile selection, grid overlay
7. **Voxel preview (Tool E)** — DONE (Session 17–18): numpy hillshade preview, instant on tile select, sun/exag sliders, block colors via pipeline button. WebGL fly-through is Phase 5.
8. **Tile inspector (Tool C)** — DONE (Session 17): coords, world range, MCA name in status bar
9. **Biome/slope layer toggles** — next up
10. **Render-status overlay** — grey/amber/green per tile, next up
11. **Live config panel (Tool B)** — thresholds as sliders with histograms
12. **Biome Studio (Tool D)** — scatterplot tuning

### Phase 3 — Global Planning
10. **Lake placement plan** — global, all types
11. **River delta plan** — tight execution, all order-3+ rivers reaching coast
12. **Aspect-based snow line** — wind exposure modulation
13. **Climate model (wind_exposure.tif)** — precipitation shadow, vegetation density gradient
14. **Geological domain map** — strata on cliff faces

### Phase 4 — Final Render
15. **Full 50k render with checkpoint system** — resumable, content-hash per tile
16. **Post-render validation suite** — all 5 checks automated
17. **MCA hot-swap into Minecraft** — final in-game walkthrough

### Phase 5 — World Quality Pass
18. **Block palette scatter rules** — no monotone surfaces, thin transitions
19. **Structure placer (Tool F)** — visual, spatial, not a config file
20. **Annotation system (Tool G)**
21. **Multi-tile voxel view** — regional landscape character evaluation

---

## Appendix — Key Numbers

- World: 50,000 × 50,000 blocks, 97 × 97 = 9,409 tiles
- Tile size: 512 × 512 blocks
- Vertical range: Y −64 (bedrock) to Y 448 (Higher Heights datapack), 512 block range
- Sea level: Y 63, raw 16-bit value 17050
- Height spline (normal polarity): gaea [0, 17050, 45000, 65496] → MC Y [−64, 63, 200, 448]
- Walking time to cross world: ~3 hours at natural speed (~4.3 m/s)
- Centre test tile: (48, 48)
- Override source resolution: 8192 × 8192 (upscaled to 50k)
- Snow line base: Y 320 ± 25 (aspect-modulated ± 35 additional)
- Tradewind direction: west → east (fixed world constant)
- 1 block = 1 metre (all bank/transition widths specified in blocks accordingly)
