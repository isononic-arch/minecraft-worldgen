# Vandir Physical Realism Refactor — Design Doc

**Status:** APPROVED — Phase 0 cleared to begin (S43, 2026-04-10)
**Session:** S43 (2026-04-10)
**Authors:** Nick (direction), Claude (architecture)
**Supersedes:** `CLAUDE.md` DIRECTION block (S43), `ARCHITECTURE_VISION.md` §"Surface decoration"
**Related:** `PROJECT_MEMORY.md`, `MASK_PIPELINE_REFERENCE.md`, `VEGETATION_MIX_SPEC.md`, `PLACEMENT_VARIATION_SPEC.md`

---

## 1. Problem Statement

Current Vandir surfaces feel **painted from noise**. Cliffs read as stone-tinted soil, transitions feel algorithmic, and every biome renders as "one stone, one grass, one tree." The Minecraft world lacks the visible *history* a real landscape carries: the wear, the erosion patterns, the wind direction, the rock that's been exposed by a glacier, the moss that grows where water pools. It looks generated, not lived-in.

The reference map is **Norterre** (50k × 30k, built World Machine → WorldPainter). Author tracks **108 layers in a spreadsheet** for ~30 landscape types. Quote: *"elevation → rivers/water → terrain material → trees/structures"* — an explicit pass pipeline. Masks imported as layers or terrain types. Multi-biome boundaries are dendritic/fractal, not clean zone edges.

Vandir has the same input stack (Gaea heightmap → precompute masks → voxel paint) but lacks the layer density, the lithology variety, and the physics-driven boundary rules to produce comparable realism.

**This refactor rewrites surface decoration end-to-end** into a layered pipeline where every block — surface and subsurface — is derived from physical signals (slope, aspect, flow, concavity, moisture, wave fetch, disturbance) rather than biome-window noise lookups.

### Goals

1. **Rock reads as rock.** Cliffs show real stone top-to-bottom. Vertical fluting on cliff faces. Distinct lithology groups per region. Grass terraces cling to moderate slopes between bare rock bands.
2. **Stone has depth.** Subsurface geology is a real lithology stack, not a uniform fill. River cuts and cave exposures reveal sediment layers. Deep geology matches surface geology.
3. **Transitions feel grown, not painted.** Biome boundaries soften organically via per-biome edge rules. Vegetation clusters by moisture/solar/disturbance, not uniform density.
4. **Every biome has its own distinct mix.** Rock palette, ground cover palette, vegetation recipe — all biome-specific, all driven by the underlying lithology group and climate signals.
5. **Agentic iteration.** I (Claude) can evaluate changes without requiring Nick in-game for every cycle. Validation happens through reviewable diagnostic artifacts, with .mca regen reserved for checkpoint gates.

### Non-Goals

- Re-running Gaea / re-processing heightmaps. Terrain geometry is fixed.
- New terrain generator. We're not building Vandir-Gen-2.
- Fixing `chk_no_bare_dirt_surface` validator false positive (parked as known-state on baseline `51_53`).
- Fixing stratification rings as a standalone task — subsumed by vertical fluting (§ 10).
- Matching Norterre's 108-layer count exactly. Target is 15-layer MVP scaling to ~40–60 as realism expands over time.

---

## 2. References

### Reference images (in `Realistic World Examples/`)

**Primary north stars:**
- `Mountain rock exposure in valley.webp` — the flagged "gold standard for the tallest snowiest mountains." Cliff stone, grass terraces, snow cap by aspect, treeline gradient. **Acceptance criterion for pilot tile (36_20) in-game validation.**
- `Boreal woodlands and river w mountains.webp` — braided river, gravel bars, forest carpeting slopes.
- `Brushy desert.webp` + `Transition zone high desert.webp` — plateau terracing, shrub clustering, dendritic erosion into flats.
- `Dune flows on rocky red desert.webp` — sand *flowing* through rocky substrate along wind paths.
- `Peninsula.webp` — beaches on exposed sides only, not uniform ringing.
- `Norterre 0410/the-best-mountains-i-have-ever-made-v0-tt6iuqayvd4f1.webp` — **vertical columnar weathering on cliff face**. Proof that vertical fluting is the right pattern, not horizontal bands.
- `Norterre 0410/render-of-my-massive-worldpainter-project-and-its-layers-v0-8gttenrfqnng1.webp` — the red/green/yellow/purple layer-mask visualization. Fractal, dendritic boundaries; layers interpenetrate along shared edges.

### Norterre author quotes (pasted by Nick)

> *"I used a spreadsheet to help keep track of the 108 layers in this world."*

> *"Two-pass method. The first pass was the general landmass, at 4m resolution. The second pass was at 1m resolution, and it included details of what each biome looked like. This is where I would add things like rocky outcroppings, erosion, etc. I did this for each biome and then composited them afterwards."*

> *"I would do it layer by layer as a whole. For example, you could start with your elevation data as your first layer, your rivers and bodies of water as your second, terrain material as your third, and trees and structures as your fourth."*

> *"The easiest way to do this is to develop a mask for these things (and other features as well), and then import them as a layer or terrain type in WorldPainter."*

The layer order Vandir adopts (§ 4) is a direct superset of this quote.

---

## 3. Architectural Principles

1. **Physical signals drive hard features, noise softens edges only.** (S41 Physical Realism Layer pattern, now made universal.) Noise is never the discriminator for a block choice — it's the ±10% jitter on an already-decided boundary.
2. **Layers, not monolithic functions.** Each decoration rule is a small, single-purpose layer. Layers compose in explicit order within a pass.
3. **Two layer types: Partition and Overlay.** Partition layers claim pixels exclusively. Overlay layers paint on already-claimed pixels without losing base. (§ 5)
4. **Scope predicates are spatial, not just categorical.** A layer's scope is a function of biome, lithology, *and* eco_grads fields (slope, aspect, flow, concavity) — not just `(biome, lithology) → bool`.
5. **Vertical slice, not horizontal.** Land all passes for one biome group (pilot: temperate mountain on tile 36_20) before rolling out to other biomes.
6. **Feature-flag risky paths.** Lithology + new column path are flagged OFF by default and validated standalone before any downstream consumer reads them.
7. **Old pipeline stays as shim.** `surface_decorator.py` remains active until the pilot biome group passes green in-game. No mid-refactor cutover.
8. **Agentic-first diagnostics.** Every layer and pass emits reviewable PNG artifacts. .mca regen only at phase checkpoints.

---

## 4. Pipeline Overview

Five passes, each owning a stack of layers, orchestrated by a new `core/surface_pipeline.py`. Pass order maps directly to Norterre's author quote.

```
Pass 0 — Regional Context        [precompute: lithology.tif]
Pass 1 — Geology / Column        [additive path in column_generator.py]
Pass 2 — Surface Block Selection [layer stack]
Pass 3 — Ground Cover            [layer stack, inline suitability]
Pass 4 — Vegetation / Schematics [layer stack, Poisson-disk weighted by inline suitability]
Pass 5 — Microdetail             [layer stack, weathering + World Studio preview cleanup]
```

Rough MVP layer count: **15**. Target trajectory over 6–12 months: **40–60**. Norterre's 108 is a multi-year endpoint, not a refactor goal.

---

## 5. Layer Protocol

Layers are Python functions (not registered via declarative JSON schema). They conform to a protocol — a shape convention enforced by type signature and unit test.

```python
# core/layers/protocol.py

from typing import Protocol, Literal
from dataclasses import dataclass
import numpy as np

LayerKind = Literal["partition", "overlay"]

@dataclass
class SurfaceContext:
    """Read-only context passed to every layer."""
    tile_x: int
    tile_z: int
    biome_grid: np.ndarray         # (512, 512) zone codes
    lithology_grid: np.ndarray     # (512, 512) lithology group, or None if flag off
    eco_grads: dict                # slope, aspect, north_factor, concavity_norm, flow, cliff_deg, etc.
    column_output: dict            # from Pass 1: surface_y, subsurface stack
    prior_surface: np.ndarray      # (512, 512) surface block names from earlier layers in this pass
    prior_ownership: np.ndarray    # (512, 512) uint8: which partition layer claimed each pixel (0 = unclaimed)

@dataclass
class LayerResult:
    """Output of a layer.apply() call."""
    modified_mask: np.ndarray      # (512, 512) bool — which pixels this layer touched
    block_output: np.ndarray       # (512, 512) block names for modified pixels
    kind: LayerKind                # partition or overlay
    layer_id: int                  # assigned by orchestrator, used for ownership tracking
    debug_meta: dict               # freeform diagnostic info (thresholds applied, masked counts)

class Layer(Protocol):
    id: str                        # "temperate_cliff_face"
    pass_num: int                  # 0-5
    priority: int                  # order within pass
    kind: LayerKind                # "partition" or "overlay"

    def apply(self, ctx: SurfaceContext) -> LayerResult: ...
```

### Composition semantics

**Partition layers** claim pixels exclusively within a pass. Rules:
- At orchestration time, later partition layers in the same pass **cannot overwrite** pixels already claimed by an earlier partition layer.
- Orchestrator enforces this: `block_output[pixel] = layer.block_output[pixel]` only where `prior_ownership[pixel] == 0`.
- `prior_ownership[pixel]` is then set to the layer's `layer_id`.
- Invariant: within a pass, ~100% of "target terrain" (e.g., land pixels for Pass 2) is claimed by exactly one partition layer. Unit test enforces this.

**Overlay layers** paint on top of already-claimed pixels.
- Orchestrator applies `block_output[pixel] = layer.block_output[pixel]` wherever `layer.modified_mask[pixel]`, regardless of prior ownership.
- `prior_ownership[pixel]` is **not** modified by overlays; overlays are tracked in a separate `overlay_touched` bitmask per pixel.
- Overlays see prior partition results in `ctx.prior_surface` and can build on them (e.g., "flute a cliff-face pixel by switching stone variant").

### Why not a declarative schema?

R1 reviewer correctly pointed out that a declarative `{block_recipe, jitter}` schema can't express real spatial logic (e.g., `band_idx = (surface_y + band_offset) % 3`). We tried that in spirit and it would lie in metadata. Layers are functions. The Protocol is a shape convention, not a JSON schema. Discoverability comes from file layout:

```
core/layers/
  protocol.py
  pass0_context/         (empty — Pass 0 is precompute, not layers)
  pass1_geology/
    bedrock_band.py
    basement_rock_by_group.py
    sediment_thickness.py
    soil_horizon.py
    river_bed.py
    lake_bed.py
  pass2_surface/
    temperate/
      cliff_face.py
      talus_apron.py
      grass_terrace.py
      weathered_top.py
      vertical_fluting.py     # overlay
      snow_cap_north.py       # overlay
    desert/
      pavement.py              # placeholder for parallel protocol test
    shared/
      beach_by_fetch.py
      river_bar.py
      lake_edge.py
  pass3_ground_cover/
    ...
  pass4_vegetation/
    ...
  pass5_microdetail/
    ...
```

An orchestrator (`core/surface_pipeline.py`) imports each pass's layer list as an ordered Python list and invokes them in sequence.

### Unit test requirements per layer

Every layer ships with at least one test:
- **Partition coverage test** (partition layers only): running the layer on a synthetic 64×64 tile where its scope predicate is satisfied everywhere yields `modified_mask.mean() ≥ 0.99`.
- **Scope isolation test**: running the layer on a synthetic tile where its scope predicate is NEVER satisfied yields `modified_mask.mean() == 0`.
- **Threshold regression test**: block_output for a fixed synthetic input is byte-identical to a committed expected output (for layers with numeric thresholds).

---

## 6. Pass Details

### Pass 0 — Regional Context (precompute, not a layer stack)

**Output:** `masks/lithology.tif` (uint8, values 1–6, NEAREST upscale semantics).

**Lithology groups:**

| ID | Name | Surface palette | Subsurface basement |
|----|------|-----------------|---------------------|
| 1 | Granitic | stone + granite + andesite + diorite + mossy cobble | granite + andesite |
| 2 | Sedimentary | sandstone + red_sandstone + smooth_sandstone + terracotta variants | sandstone + red_sandstone |
| 3 | Basaltic | basalt + smooth_basalt + blackstone + polished_blackstone + magma_block | basalt + blackstone |
| 4 | Limestone | calcite + tuff + dripstone_block + smooth_stone | calcite + tuff |
| 5 | Deepslate-metamorphic | deepslate + cobbled_deepslate + tuff + polished_deepslate | deepslate |
| 6 | Mossy-temperate | stone + cobblestone + mossy_cobblestone + mossy_stone_bricks | stone + cobblestone |

**Assignment rule (initial, hand-tunable):** Direct LUT on override zone code + elevation bucket + region (optional). No hydrology signals, no flow-dependency — keeps Pass 0 deterministic and debuggable.

**Draft assignment for Vandir's current zones** (to be refined in Phase 0.5):
- `SAND_DUNE_DESERT`, `DRY_OAK_SAVANNA`, `ARID_STEPPE`, desert_transition → Sedimentary (2)
- `MIXED_FOREST`, `TEMPERATE_RAINFOREST`, `TEMPERATE_CONIFEROUS_FOREST` → Mossy-temperate (6)
- `BOREAL_TAIGA`, `SNOWY_BOREAL_TAIGA` → Granitic (1) with deepslate basement at Y < 0
- `ALPINE_*`, `ROCKY_PEAKS`, `SNOW_CAPS` → Granitic (1) on upper elevations, Deepslate (5) on basement
- `FROZEN_FLATS`, `ARCTIC_TUNDRA` → Granitic (1) with limestone dripstone patches
- Volcanic regions (if any) → Basaltic (3)
- Coastal limestone reefs (if any) → Limestone (4)

**Validation metrics (before flag flips ON anywhere):**
1. Band count distribution per elevation bucket (expect ~3–5 distinct groups represented in 3+ elevation buckets).
2. Edge-clip detection at water carve boundaries (no lithology should abruptly terminate at a river without matching biome boundary).
3. Cross-tile alignment: lithology on tile `(X,Z)` right edge == tile `(X+1,Z)` left edge at every row.
4. Visual inspection on one cliff cross-section via new `diag_cliff_crosssection.py` (Phase 0.5).

All four checks added to `tools/validate_masks.py` before the feature flag is enabled.

---

### Pass 1 — Geology / Column (additive path in existing column_generator.py)

**No in-place rewrite.** `column_generator.py` gains two optional parameters:

```python
def fill_column(
    ...existing args...,
    lithology_tile: np.ndarray | None = None,
    sediment_thickness_tile: np.ndarray | None = None,
    use_new_geology: bool = False,   # feature flag
) -> ColumnResult:
    if not use_new_geology or lithology_tile is None:
        return _fill_column_current(...)   # byte-identical to today
    return _fill_column_with_geology(..., lithology_tile, sediment_thickness_tile)
```

**Golden-output unit test:** with `use_new_geology=False`, output must equal pre-edit baseline at byte level on a fixed sample of 100 columns from tile 36_20. Runs every commit. Prevents silent drift.

**Layers inside `_fill_column_with_geology()`** (5–8 sublayers, all reading `ctx.lithology_grid`):

1. **bedrock_band** — Y -64 to -60, bedrock + deepslate scatter. Replaces current bedrock layer.
2. **basement_rock_by_group** — Y -60 to basement_top, block type from lithology group's "basement" palette column.
3. **sediment_thickness** — above basement, thickness = `f(concavity_norm, flow_accum)`. Concave + high-flow = thicker sediment; convex ridge = minimal. Block type: gravel / sand / dirt based on flow magnitude.
4. **soil_horizon** — above sediment, depth = `f(biome, slope)`. Shallow on steep slopes (cliffs: 0–1 blocks of soil before rock). Deep on flat biomes (forests: 3–4 blocks of dirt).
5. **river_bed** — where `river_meta != NONE`, overrides sediment with gravel/sand/clay by river class.
6. **lake_bed** — where `hydro_lake != 0`, overrides sediment with silt/gravel/dirt by lake depth.
7. **cave_exposure** (future; Phase 2) — if cave carver finds rock, emit exposed lithology-appropriate block.
8. **vertical_fluting_consumer** — reads Pass 2's fluting phase signal (injected via `ColumnResult`) and applies variant substitution to cliff-face columns. See § 10.

**Baseline strategy:**
- Snapshot `tests/baselines/3x3/36_20/` BEFORE any column edit. (36_20 is in-game validated, known-good.)
- Additional snapshot: 5 sample columns on 36_20 as printable block stacks (JSON), for human-inspectable diff.
- Do NOT snapshot 24_80 as a regression baseline for column work — current desert subsurface is a known-unknown. Desert subsurface gets visually inspected DURING Phase 1, not baseline-guarded.

**Risk (from R2):** if cave carvers exist and read the stone/subsurface boundary, lithology changes could produce different cave shapes. Mitigation: grep for cave carving code paths before Phase 1 starts. If none exist in Vandir today, this risk collapses.

---

### Pass 2 — Surface Block Selection

Replaces the bulk of `surface_decorator.py`'s surface-block logic. Each layer is a Python function in `core/layers/pass2_surface/`.

**MVP layers (temperate mountain pilot + shared):**

| Layer | Type | Scope | Inputs | Output rule |
|-------|------|-------|--------|-------------|
| `temperate_cliff_face` | partition | temperate biomes, `cliff_deg ≥ 35°` | lithology, cliff_deg, aspect | Rock mix from lithology group (fine-scatter 70% primary + 20% secondary + 10% accent), ±10% edge jitter |
| `temperate_talus_apron` | partition | temperate, below cliff_face pixels, `18° ≤ cliff_deg < 35°`, concave_norm > threshold | lithology, concavity, slope | Cobblestone + gravel fine-scatter, transitioning from cliff_face above |
| `temperate_grass_terrace` | partition | temperate, `slope_class == moderate`, not cliff or talus | biome, slope, north_factor | Grass block + coarse dirt patches; grass dominates on north_factor >0.3, coarse dirt on south_factor |
| `temperate_weathered_top` | partition | temperate, `slope_class == flat`, high elevation, exposed | concavity, wind_exposure | Mossy cobble + stone + grass block mix, windswept |
| `vertical_fluting` | **overlay** | any biome, `cliff_deg ≥ 35°`, already claimed by cliff_face layer | aspect_smoothed, cliff_tangent_direction | Phase-modulated stone variant substitution, stripe width 4 ±1 block. See § 10. |
| `snow_cap_north` | **overlay** | elevation ≥ snow_line, `north_factor > 0.4` | north_factor, elevation | Snow block substitution |
| `beach_by_fetch` | partition | shoreline, new `wave_fetch.tif` > threshold | wave_fetch, shoreline_distance | Sand primary, gravel secondary; width scales with fetch |
| `river_bar` | partition | inside meander bends, low flow | flow, concavity, bend curvature | Gravel + sand + coarse dirt scatter |
| `lake_edge` | partition | lake fringe (2px band) | hydro_lake_wl, terrain | Sand + gravel + dirt, width tuned to slope |

Rough count: 9 layers for MVP pilot, with temperate group + shared. Desert_pavement and boreal_moss_carpet get their own layer files in Phase 5 (horizontal rollout).

**Parallel protocol test (R2 #3 mitigation):** during Phase 2 (temperate pilot), implement ONE desert layer (`desert_pavement`) to prove the Layer Protocol generalizes across biome groups before we lock it in.

**Invariant:** after Pass 2 runs on a temperate-mountain tile, `(prior_ownership != 0).mean() ≥ 0.99` on land pixels. Enforced by unit test on pilot tile.

---

### Pass 3 — Ground Cover

Replaces ground cover logic in `surface_decorator.py`. Each layer emits ground-cover blocks (leaf litter, moss carpet, pale moss, grass variants, bushes, tall grass, ferns, flowers).

**Layers (MVP, ~5):**

| Layer | Type | Scope | Density rule |
|-------|------|-------|--------------|
| `temperate_forest_floor` | partition | temperate forested biomes, grass_block surface | `density = base × (0.5 + 0.5 × moisture_idx) × (0.7 + 0.3 × north_factor)` |
| `alpine_grass_meadow` | partition | alpine biomes, grass_block surface | `density = base × (1 - north_factor × 0.5)` (sun-facing denser) |
| `riparian_lush` | partition | within 6 blocks of river centerline, temperate | `density = base × 1.5`, fern + tall grass mix |
| `boreal_moss_carpet` | partition | boreal biomes, flat slopes | `density = base × (0.5 + 0.5 × concavity_norm)` |
| `edge_softening` | overlay | within N blocks of biome boundary (see § 11) | additive edge recipe per biome |

**Inline suitability (R2 #8 mitigation):** no new precompute mask. Each layer computes its density field from existing eco_grads + `moisture_idx` derived inline via `scipy.ndimage.distance_transform_edt` on water masks. If wall-time becomes a problem, promote to precompute later.

---

### Pass 4 — Vegetation / Schematics

Replaces the placement code in current decoration. Schematic recipes (existing config) stay; **placement sampling** changes.

**MVP layers (~4):**

| Layer | Type | Scope | Placement rule |
|-------|------|-------|----------------|
| `temperate_tree_canopy` | partition | temperate biomes, grass_block | Poisson-disk weighted by `suitability = moisture × (1 - steepness) × (1 - disturbance)` |
| `alpine_treeline` | partition | alpine, elevation-band | Density falls off linearly from `treeline_low` to `treeline_high` |
| `disturbance_succession` | partition | windthrow + old floodplain masks | Early-succession recipe (birch sapling, tall grass, ferns) instead of climax trees |
| `riparian_trees` | partition | within 4 blocks of river centerline | Willow / alder / birch cluster, denser than upland |

**Poisson-disk weighted by suitability:** standard Poisson-disk with per-candidate rejection probability = `1 - suitability(x, z)`. Gives visible clustering without random holes.

---

### Pass 5 — Microdetail + World Studio preview cleanup

**Microdetail layers (~5, all overlay):**

| Layer | Scope | Effect |
|-------|-------|--------|
| `water_edge_staining` | within 2 blocks of water surface | Mossy cobble / algae patches on rock near waterline |
| `south_face_bleaching` | rock pixels with high solar exposure | Smooth sandstone / bleached variants |
| `north_face_moss` | rock pixels with north_factor > 0.6, humid biomes | Mossy cobble scatter |
| `frost_heave` | cold biomes, flat slopes | Occasional stone/cobble poking through grass |
| `debris_apron_rocks` | below cliff faces, in talus layers | Small boulder scatter (schematic, not block-level) |

**World Studio preview cleanup:** strip the noise-based default render layers (slope rocks, moisture, etc.) from `tools/world_studio.py` preview. Keep only the base surface block render driven by the new pipeline. Nick's call — these layers produce meaningless noise and are actively misleading during iteration.

---

## 7. Phase 0.5 — Agentic Diagnostic Tooling

**Why this exists:** The refactor needs iterative visual evaluation, but I (Claude) can't fly into Minecraft. .mca regen + test-world loading takes ~20 minutes per cycle and requires Nick's input. To iterate autonomously, I need diagnostic renderers that produce reviewable PNG artifacts Nick can spot-check between .mca checkpoints.

**Axes of evaluation: BOTH vertical AND horizontal matter.** Top-down checks catch biome transitions, pixel ownership, ground cover density, river corridors, boundary softening. Cross-sections catch lithology stacks, soil horizon depth, vertical fluting stripe rendering, river-bed geometry, cliff column integrity. Every phase checkpoint produces both axes — neither alone is sufficient.

**New diagnostic tools:**

1. **`diag_cliff_crosssection.py`** — *vertical axis*
   - Input: tile coords + sample line (2 endpoints in tile-local coords)
   - Output: PNG showing vertical slice through column_output along the line. Y axis = block Y, X axis = distance along sample line. Each cell = block type colored by a stone palette.
   - Use: evaluate lithology stacks, vertical fluting, sediment/soil horizon layering, river-bed cross-sections.
   - This is the **primary vertical-axis tool** for Pass 1 and § 10 work.
   - Default sample lines per pilot tile committed to `tests/diag_lines/{tile}.json` so reruns are reproducible and baselineable.

1b. **`diag_topdown_blocks.py`** — *horizontal axis*
   - Input: tile coords (or 3×3 range)
   - Output: PNG of surface block type at Y = surface_y, colored by a canonical block palette. Same resolution as current `diag_layers_breakdown.py` stitched_blocks panel but focused on block *identity* not layer ownership.
   - Use: catch bare dirt, grass/stone mix, riparian bank reads, ground cover density, beach widths, boundary softening.
   - This is the **primary horizontal-axis tool** for Pass 2/3/4 work.

2. **`diag_layer_ownership.py`**
   - Input: tile coords, pass number
   - Output: PNG with each partition layer colored distinctly, showing which pixels each claimed.
   - Use: debug pixel ownership conflicts, verify coverage invariants, catch unclaimed pixels.

3. **`diag_suitability_field.py`**
   - Input: tile coords, layer id
   - Output: PNG of a single layer's density/suitability field as a grayscale heatmap.
   - Use: tune ground cover and vegetation layer parameters without rerunning the full pipeline.

4. **`diag_layer_breakdown.py`** (extend existing `diag_layers_breakdown.py`)
   - Add N+1 panels for new layers added in each pass.
   - Add "pixel ownership" as a dedicated panel.

5. **`diag_fluting_phase.py`**
   - Input: tile coords
   - Output: PNG showing computed cliff tangent direction (as a hue field) and the resulting stripe phase (as a value field) across the tile.
   - Use: tune vertical fluting parameters.

**Artifact handling:** all tools write to `diag_output/{tile_x}_{tile_z}/{tool_name}.png`. I (Claude) save them to the workspace folder and present via `mcp__cowork__present_files` at phase checkpoints so Nick can review without opening Minecraft.

**Runtime budget:** each tool completes in < 60s on a single tile. The full diag suite for a tile should take < 5 minutes total. This replaces ~20 minutes of .mca regen + test-world validation for most iteration cycles.

---

## 8. Vertical Fluting — Detailed Spec

(Special callout because of Nick's observation: vertical stripes don't render top-down, they only show from the side.)

### Problem

Cliff faces in-world look like uniform stone when viewed from the side, because every column along the cliff's tangent direction emits the same stone variant. Real cliffs show vertical columnar weathering — distinct vertical lines of different color/variant along the cliff's face, created by differential erosion along joint planes.

### Observation (from Nick)

A cliff face is seen in-world from the *side*, not from top-down. The side-view is composed of the side-exposed blocks of adjacent columns along the cliff's horizontal tangent. If two adjacent columns emit the same stone variant, the side shows uniform stone. If they emit *different* variants, the side shows vertical stripes.

**Implication:** "Vertical fluting" = **stone variant modulation along the cliff's horizontal tangent direction**, applied to the whole column (or at least the cliff-exposed portion). The stripe is along Y, but the CHOICE happens at (x, z) — the column picks one variant, the next column picks a different one.

### Algorithm

1. **Pass 2 `vertical_fluting` overlay layer runs after `temperate_cliff_face` partition.**
2. For each cliff-face pixel `(x, z)`:
   a. Compute the local 2D slope gradient `(gy, gx)` from smoothed surface_y (3×3 mean filter to kill 1:8 noise).
   b. Compute the cliff tangent direction `(-gx, gy)` (perpendicular to slope direction, in the horizontal plane).
   c. Project the pixel's (x, z) onto the tangent: `phase = (x × tangent_x + z × tangent_z)`.
   d. Determine stripe variant: `variant_idx = (phase // STRIPE_WIDTH) % N_VARIANTS` where `STRIPE_WIDTH = 4` blocks and `N_VARIANTS = len(lithology_group.cliff_palette)`.
   e. Add ±1 block of jitter per pixel to break perfect straight lines.
3. Emit `block_output[x, z] = lithology_group.cliff_palette[variant_idx]`.
4. Pass 1's `vertical_fluting_consumer` layer reads this variant signal and applies the same variant to the basement stone *for the full vertical extent of the cliff-exposed column* (from surface down to where the column stops being cliff-exposed, ~10–30 blocks).

### Why this is a cross-pass concern

The variant signal is computed in Pass 2 from horizontal context (tangent direction), but has to be applied to the *column* in Pass 1 to render as vertical stripes from the side. So Pass 2 runs first to compute the variant per (x,z), then Pass 1 re-runs (or the column writer is told about the variant) to paint the column. This is the one place where Pass 1 ↔ Pass 2 has a backwards dependency.

**Implementation:** Pass 2's `vertical_fluting` layer writes its variant decision into a shared `ctx.variant_hints` dict. When Pass 1 column_generator paints cliff-exposed columns, it reads `variant_hints[x, z]` and substitutes the basement stone variant accordingly.

### Validation

- `diag_cliff_crosssection.py` is the ONLY way to evaluate this without flying into Minecraft. Run it on tile 36_20 with a sample line crossing 3–4 cliff faces. Expect visible vertical stripes in the cross-section PNG.
- Budget: **2–3 validation + re-tune cycles**. Stripe width 4 may be wrong; jitter may be too much/too little; tangent direction may snap to cardinals on noisy heightmap.

---

## 9. Boundary Softening — Per-Biome Symmetric Rules

(Replaces the O(N²) per-adjacency-pair proposal from R1.)

### Rule shape

Each biome declares one edge-softening recipe in `config/thresholds.json`:

```json
"boundary_edge_softening": {
  "TEMPERATE_RAINFOREST": {
    "edge_width_blocks": 8,
    "ground_cover_additions": {"moss_block": 0.05, "pale_moss_carpet": 0.03},
    "schematic_density_multiplier": 1.2,
    "block_jitter": 0.1
  },
  "BOREAL_TAIGA": {
    "edge_width_blocks": 10,
    "ground_cover_additions": {"lichen": 0.08},
    ...
  }
}
```

### Application rule

At the boundary between biome A and biome B:
- Biome A's rule applies ONLY to pixels on A's side (within A's `edge_width_blocks` of the boundary).
- Biome B's rule applies ONLY to pixels on B's side (within B's `edge_width_blocks` of the boundary).
- The seam itself is the boundary; each side owns its own softening. No overlap, no tiebreak needed.

### Directional "side-of" computation

For each land pixel P near a biome boundary:
- Find P's biome label.
- Find P's nearest non-self neighbor pixel within a `max(edge_width)` radius (cheap with `scipy.ndimage.distance_transform_edt` producing both distance and indices).
- If distance ≤ P's biome's `edge_width_blocks`, apply P's biome's rule.

### Scope

Edge softening is implemented as a Pass 3 `edge_softening` overlay layer that modifies ground cover density additively. Does NOT touch surface blocks (which are already lithology-driven and don't need softening).

---

## 10. Vertical Fluting ↔ Stratification Rings

The parked stratification-rings bug (horizontal bands forming concentric rings on conical hills in `_apply_desert_rock_palette()` step 6) is **superseded by vertical fluting**. The fix isn't "move the band axis to along-slope" — it's "replace band concept with vertical fluting driven by aspect-aligned cliff tangent". Horizontal band logic is removed entirely in the Pass 2 rewrite.

**Action:** delete `_apply_desert_rock_palette()` stratification step in favor of a new `desert_vertical_fluting` layer (Phase 5 horizontal rollout). Until then, the current bug remains cosmetic and parked.

---

## 11. Phase Plan

Every phase has: a baseline snapshot taken *before* any edit, unit tests, a feature flag, and a diagnostic deliverable.

### Phase 0 — Layer Protocol scaffolding + Diagnostic Tooling (week 1)

**Deliverables:**
- `core/layers/protocol.py` with `SurfaceContext`, `LayerResult`, `Layer` protocol, composition semantics.
- `core/surface_pipeline.py` orchestrator skeleton (imports empty layer lists, walks them, enforces partition/overlay rules). Not yet called from production code.
- `diag_cliff_crosssection.py`, `diag_layer_ownership.py`, `diag_suitability_field.py` (minimum three).
- Unit tests for partition coverage and overlay behavior on synthetic tiles.

**Exit criteria:** orchestrator runs on an empty layer list over a synthetic tile and produces correct zero-output. Diagnostic tools run on tile 36_20 and emit PNGs. No production code touched.

**Risk:** low. Scaffolding only.

---

### Phase 0.5 — Lithology precompute (week 1–2)

**Deliverables:**
- `tools/build_lithology.py` — generates `masks/lithology.tif` from `override.tif` + `height.tif` via hand-tuned LUT.
- Draft assignment (§ 6 Pass 0 table) committed to `config/thresholds.json`.
- 4 new validation checks added to `tools/validate_masks.py`.
- Visual check via `diag_cliff_crosssection.py` on 36_20 showing lithology bands at cliff faces.

**Exit criteria:** all 4 validation metrics PASS on lithology.tif. Visual inspection shows plausible lithology distribution on 36_20, 24_80, 59_53. **Lithology feature flag remains OFF.** No downstream consumer reads it yet.

**Risk:** low. Standalone precompute, no pipeline changes.

---

### Phase 1 — Column additive path (week 2)

**Deliverables:**
- `core/column_generator.py` gains optional `lithology_tile`, `sediment_thickness_tile`, `use_new_geology` parameters.
- `_fill_column_with_geology()` inner function implementing sublayers 1–7 from § 6 Pass 1.
- Golden-output unit test: `use_new_geology=False` produces byte-identical output to pre-edit baseline on 100 sample columns from 36_20.
- 36_20 baseline (`tests/baselines/3x3/36_20/`) captured BEFORE any edit.
- 5 sample columns on 36_20 snapshot as printable block stacks (JSON).
- `diag_cliff_crosssection.py` validates new geology path produces plausible vertical lithology on 36_20 when flag ON.

**Exit criteria:**
- Feature flag OFF: pipeline output is byte-identical to pre-edit baseline on 36_20 3×3. `--baseline` diff reports 0 new failures.
- Feature flag ON: cliff cross-sections on 36_20 show real lithology stack (bedrock → basement → sediment → soil horizon).

**Risk:** medium. Column_generator is in-game validated and the regression surface is invisible from top-down. Feature flag and golden-output test contain the blast radius.

---

### Phase 2 — Temperate Mountain Pass 2 (week 3)

**Deliverables:**
- 9 Pass 2 layers from § 6 table, implemented in `core/layers/pass2_surface/temperate/` + `shared/`.
- `core/surface_pipeline.py` wired as the Pass 2 orchestrator.
- `surface_decorator.py` gains a feature flag `use_new_surface_pipeline` — default OFF. When ON, delegates to `surface_pipeline.py` for temperate-mountain biomes only; falls back to current logic elsewhere.
- Parallel protocol test: `desert_pavement` layer implemented (1 layer, not full pass) to prove protocol generalizes.
- Unit tests: partition coverage ≥ 99% on 36_20 land pixels; `desert_pavement` scope isolation on 24_80.
- `diag_layer_ownership.py` output on 36_20 shows all 9 layers cleanly partitioning the terrain.
- `diag_cliff_crosssection.py` output on 36_20 shows vertical fluting on cliff faces.

**Exit criteria:**
- Feature flag ON for temperate-mountain biomes produces PNGs Nick reviews and approves.
- Regression baseline on 48_48 (ocean) + 51_53 (river/forest) still green.
- .mca regen at end of phase: tile 36_20 in-world comparison to `Mountain rock exposure in valley.webp` north star.

**Risk:** high. Most of the creative work lives here. Most likely slip point.

---

### Phase 3 — Temperate Mountain Pass 3 + 4 (week 4)

**Deliverables:**
- 5 Pass 3 ground cover layers from § 6 table.
- 4 Pass 4 vegetation layers with Poisson-disk weighted by inline suitability.
- `diag_suitability_field.py` output on 36_20 for each vegetation layer.
- `PLACEMENT_VARIATION_SPEC.md` and `VEGETATION_MIX_SPEC.md` reviewed for recipe content; recipes fed into the new layer functions without re-specifying.

**Exit criteria:**
- Ground cover density fields visible in diag output show clustering near water / disturbance / north faces.
- Vegetation placements visible in top-down preview show clustering patterns, not uniform distribution.
- .mca regen on 36_20, in-world comparison to `Boreal woodlands and river w mountains.webp` for riparian clustering read.

**Risk:** medium. Ground cover and vegetation are high-visibility but relatively contained.

---

### Phase 4 — Pilot decision gate (end of week 4)

**Deliverables:**
- In-game validation session: Nick + Claude review tile 36_20 against the north star reference images.
- Decision: roll out horizontally to other biome groups (Phase 5) OR pivot.

**Exit criteria:** Nick signs off on the pilot or names specific failures.

---

### Phase 5 — Horizontal rollout (week 5+)

**Deliverables per biome group:**
- Complete Pass 2 layer set (desert: `desert_cliff_face`, `desert_pavement`, `desert_vertical_fluting`, etc.; boreal: `boreal_cliff_face`, `boreal_moss_carpet`, etc.; coast: `beach_by_fetch` + marine variants).
- Pass 3 ground cover layers per biome.
- Pass 4 vegetation layers per biome.
- Tile-specific baselines: 24_80 (desert), 59_53 (windthrow/boreal), 25_72 (flat sand), 16_73 (meander).
- Old `surface_decorator.py` deleted after all biome groups pass.

**Exit criteria per biome group:** in-world comparison passes against the relevant north star reference image.

**Risk:** known. This is where the per-biome tuning debt lives. Pace: 1 biome group per week, realistic budget 3–5 weeks total.

---

### Phase 6 — World Studio preview cleanup (parallel, low priority)

**Deliverables:**
- Strip noise-based default render layers from `tools/world_studio.py`.
- Re-wire preview to render only surface blocks from the new pipeline.

**Exit criteria:** World Studio preview runs and shows the new pipeline's output instead of the legacy noise defaults.

**Risk:** low but distracting — parallelize with Phase 5.

---

## 12. Risk Register

Compiled from R1 + R2 adversarial review. Each risk has a status and mitigation.

| # | Risk | Severity | Status | Mitigation |
|---|------|----------|--------|-----------|
| R1-1 | Layer count 45–65 → 4–8 week calibration grind | H | **Resolved** | MVP target cut to ~15 layers; 40–60 is multi-year trajectory, not refactor deliverable |
| R1-2 | Declarative LayerRegistry can't express spatial logic | H | **Resolved** | Skipped declarative schema; layers are Python functions conforming to a Protocol |
| R1-3 | O(N²) per-adjacency boundary rules | H | **Resolved** | Replaced with per-biome symmetric rules; 15–20 entries instead of 400 |
| R1-4 | Vertical fluting "1 day" is naive | M | **Resolved** | Budgeted 2–3 validation cycles; simple 2D gradient algorithm, not eigenvalue ridge tracing |
| R1-5 | Column generator rewrite = highest regression risk | H | **Resolved** | No in-place rewrite; additive path with feature flag + golden-output test |
| R1-6 | Phase ordering creates reverse-dependency chain | H | **Resolved** | Lithology + column work are feature-flagged, validated standalone before downstream consumers read them |
| R1-7 | 4 weeks of code, 0 visible wins until week 4 | H | **Resolved** | Vertical slice on tile 36_20; phase gates produce diagnostic artifacts weekly |
| R1-8 | Suitability precompute unvalidated | M | **Resolved** | No new precompute; inline suitability in vegetation layers |
| R2-1 | Layer Protocol pixel ownership chaos | H | **Resolved** | Explicit partition/overlay split; unit test on coverage invariant |
| R2-2 | Lithology validation metrics undefined | M | **Resolved** | 4 metric checks in `validate_masks.py` before flag flip |
| R2-3 | Temperate-tuned layer ordering may not compose with desert | M | **Resolved** | Parallel `desert_pavement` protocol test during Phase 2 |
| R2-4 | Column additive path feature flag fragility | M | **Resolved** | Golden-output unit test on flag-OFF path |
| R2-5 | In-game validation bandwidth is week 3–4 bottleneck | H | **Resolved** | Phase 0.5 diagnostic tooling replaces most .mca cycles |
| R2-6 | Per-biome boundary rules overlap at seams | M | **Resolved** | Directional side-of computation — each side owns its own softening |
| R2-7 | 24_80 baseline snapshot gotcha (current subsurface may be wrong) | M | **Resolved** | Baseline 36_20 (known-good) instead for column work |

**Remaining open risks (not yet resolved):**

| Risk | Severity | Notes |
|------|----------|-------|
| Cave carver interaction with new subsurface lithology | Unknown | Need to grep `core/` for cave carving code paths in Phase 1. If none exist, collapses to zero. |
| `wave_fetch.tif` precompute cost | Low | Directional distance transform over water. Estimated moderate cost. Measure in Phase 2. |
| Phase 5 per-biome tuning debt | Medium | Known unknown; pace is 1 biome/week, tolerance for slip |
| World Studio refactor may break existing workflows | Low | Parallel work in Phase 6; Nick accepted this cost |

---

## 13. Safety Rails

1. **Baseline before every edit.** Any touch to a `core/` path without an existing 3×3 baseline covering it requires a new baseline snapshot first.
2. **Feature flags on all risky paths.** Lithology, new column geology, new surface pipeline — all flag-OFF by default. Production pipeline runs old code until each flag is flipped per phase.
3. **Golden-output tests on flag-OFF paths.** Byte-identical verification that flag-OFF matches pre-edit baseline on column_generator's 100-column sample.
4. **Partition coverage invariant enforced by unit test.** Any pass that fails the ≥99% partition coverage check on its pilot tile is a blocker.
5. **Old code stays as shim until pilot passes.** `surface_decorator.py` is not deleted until Phase 4 decision gate signs off the pilot.
6. **.mca regen only at phase checkpoints.** Iteration happens through diagnostic PNGs. Per-phase .mca regen once.
7. **No 50k runs.** Ever.
8. **Failure logging before retries.** Per `CLAUDE.md` workflow rule: after a failed fix, write timestamp + what + why to `memory/project_vandir_status.md` BEFORE retrying. 2 failures on the same symptom = STOP and investigate or ask Nick.

---

## 14. Acceptance Criteria

### Pilot tile (36_20, end of Phase 4)

Nick reviews tile 36_20 in-world and compares to `Realistic World Examples/Mountain rock exposure in valley.webp`. The pilot passes if:

1. Cliff faces read as stone top-to-bottom, not as stone-tinted soil.
2. Vertical fluting visible on cliff faces from ground view — distinct vertical stripes of different stone variants.
3. Grass terraces cling to moderate slopes between cliff bands, not as uniform grass fields.
4. Snow cap concentrates on north-facing upper elevations, not uniform altitude.
5. Treeline is a gradient (dense → sparse → bare), not a hard edge.
6. Ground cover shows visible clustering (moss near water, lichen on north faces), not uniform density.
7. Vegetation (tree schematic placements) shows clustering around moisture / disturbance signals.
8. No visible regressions vs. baseline 36_20 3×3.

Anything less is a failure → Phase 5 does not begin until Phase 2–3 are re-tuned.

### Horizontal rollout (per biome group, Phase 5)

Per biome group, the matching north star image from `Realistic World Examples/` serves as the acceptance reference. Pace target: 1 biome group per week; 3–5 weeks total for desert, boreal, coast.

---

## 15. Out of Scope (Parked)

- Norterre's 108-layer count as a literal target. Aspirational trajectory only.
- Two-pass heightmap in World Machine style. Vandir's Gaea heightmap is fixed.
- Per-adjacency-pair boundary rules. Only per-biome symmetric rules.
- `chk_no_bare_dirt_surface` validator false positive on `51_53`. Locked as known-state.
- Stratification rings bug as a standalone fix. Subsumed by vertical fluting (§ 10).
- Caves / cave carvers (re-evaluate during Phase 1 if Vandir has cave carving at all).
- Wind / weather simulation.
- Seasonal variation (snowfall changes, leaf-drop cycles).
- Per-structure weathering (buildings).

---

## 16. Open Questions — RESOLVED (S43, 2026-04-10)

1. **Cave carving?** **No.** Everything below surface is solid. R1-5 (cave-carver interaction) collapses. Subsurface lithology pass owns the full column below the soil horizon with no carver coordination needed.
2. **`wave_fetch.tif` at 1:8?** **In scope.** Directional distance transform over water at 6250×6250 is cheap (~seconds with scipy). Claude will precompute it in Phase 0.5 alongside lithology. Used by coastal beach width in Pass 2+.
3. **Horizontal rollout pace?** **Autopilot.** Once pilot tile 36_20 passes acceptance, Claude drives biome-group expansion at its own cadence, logging each group completion to `memory/project_vandir_status.md`. Nick vetoes on review.
4. **Schematic index + recipes?** **Unchanged.** Index and recipe files remain exactly as-is. Only the placement *sampler* changes (algorithmic → suitability-weighted Poisson-disk from inline vegetation suitability fields).

---

## 17. Signoff

- [x] Nick: Plan approved to begin Phase 0 (S43, 2026-04-10).
- [x] Nick: Open questions 1–4 answered (S43, 2026-04-10).
- [ ] Claude: Phase 0 baseline + diagnostic tool skeleton ready to commit on Phase 0 kickoff.
