# Vandir Physical Realism Refactor — Design Doc

**Status:** APPROVED (S43, 2026-04-10) — Phase 0 cleared. Updated with Nick's §1–6 review feedback. §7+ awaiting second review pass.
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
9. **Tradewinds blow west → east (270°).** Every wind-influenced layer — windthrow, wave fetch, dune orientation, snow lee accumulation, leaf-litter drift, sand scour, weathering bleach asymmetry — uses this as ground truth. West-facing slopes are windward (exposed, scoured, dryer); east-facing slopes are leeward (sheltered, accumulation-biased, wetter leeside). This is already baked into CLAUDE.md world constants; the refactor makes every wind-aware layer reference a single `core/wind_model.py` helper rather than rolling its own aspect math. Gets this wrong and the map's whole weathering story runs backwards.
10. **Block-texture reality check.** Before any block joins a palette, its actual in-game texture is the deciding vote, not its name. Names are misleading (`smooth_stone` is flat-gray and reads as bunker concrete; `mud` is near-black with a subtle water sheen; `packed_mud` is arid dry-clay brown; `rooted_dirt` shows pale root veins). When I'm not confident a block will read right in context, I flag it, propose alternatives, and wait for Nick's call rather than shipping it into a palette.

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
| 1 | Granitic | **andesite (primary temperate light rock)** + stone + granite + diorite + mossy cobble | granite + andesite |
| 2 | Sedimentary | sandstone + red_sandstone + smooth_sandstone + terracotta variants | sandstone + red_sandstone |
| 3 | Basaltic | basalt + smooth_basalt + blackstone + magma_block *(no polished_blackstone)* | basalt + blackstone |
| 4 | Limestone | calcite + tuff + dripstone_block + stone *(no smooth_stone)* | calcite + tuff |
| 5 | Deepslate-metamorphic | deepslate + cobbled_deepslate + tuff *(no polished_deepslate)* | deepslate |
| 6 | Mossy-temperate | **andesite** + stone + cobblestone + mossy_cobblestone *(no mossy_stone_bricks)* | stone + cobblestone |

**No polished/brick-ified blocks** in any lithology palette. They read as "built" rather than natural and break immersion on cliff faces. Rule of thumb: if the block has a visible chiseled grid or mortar lines, it's out.

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
| `river_bar` | partition | inside meander bends, low flow | flow, concavity, bend curvature | Gravel + sand + coarse dirt scatter *(in arid / sedimentary zones only — temperate uses `temperate_riparian_fringe` below)* |
| `lake_edge` | partition | lake fringe (2px band) | hydro_lake_wl, terrain | *(arid / sedimentary)* sand + gravel + dirt |
| `temperate_riparian_fringe` | partition | temperate zones, within 3–5 blocks of river centerline OR inside lake fringe band | flow, concavity, moisture_idx | Brown/mud/dirt palette (see below) with matched noise profile from legacy riparian noise mix. Claims lake_edge + river_bar pixels in temperate zones. |
| `temperate_forest_surface` | partition | temperate forested biomes (`MIXED_FOREST`, `TEMPERATE_RAINFOREST`, `TEMPERATE_CONIFEROUS_FOREST`), `slope_class == flat` or `moderate`, not claimed by cliff/talus/riparian | biome, tree_density_hint, north_factor | Grass-dominant mix with legacy mixed-forest noise scale driving coarse_dirt / podzol / rooted_dirt scatter |

**Temperate riparian fringe palette** (Pass 2 default for temperate lake/river edges; supersedes earlier sand+gravel default):

Primary (60–70%): `dirt`, `coarse_dirt`, `rooted_dirt`. These read as muddy waterline soil without the arid gravel-bar feel.
Secondary (20–30%): `podzol` in shaded bends (north-facing, dense canopy nearby), `packed_mud` sparingly in high-flow scour spots.
Accent (≤10%): `gravel` at the immediate waterline only (1px band), `clay` in still-water lake fringes.
**Block-texture verification (pulled from `meta/versions/1.21.10/1.21.10.jar` on 2026-04-10):**

| Block | Texture read | Riparian verdict |
|-------|--------------|------------------|
| `dirt` | Classic warm mid-brown, slightly varied | ✅ Safe baseline, primary |
| `coarse_dirt` | Darker grittier brown | ✅ Good companion, primary |
| `rooted_dirt` | Warm brown with pale beige root veins | ✅ **Only under tree canopy / next to willow-alder clusters.** Out of place on open lake edges. Guarded by a `near_canopy_hint` predicate. |
| `podzol` (top) | Dark brown with visible green moss/needle bits | ✅ Shaded bends + dense-canopy north-facing bank |
| `packed_mud` | Warm dry tan, caked-clay read | ❌ **Removed from temperate.** Arid riparian only (swap to `arid_riparian_fringe` in Phase 5). |
| `mud` (1.19+) | Very dark brown-gray with subtle wet sheen | ⚠️ **Accent only, narrow bands** — too dark as primary. Test render required before the accent band > 1 pixel wide. |
| `gravel` | Light gray with warm specks | ❌ **Removed from temperate.** Too light to blend with dark banks. Arid riparian only + 1-pixel immediate waterline. |
| `clay` | Soft pale blue-gray, fine texture | ✅ Still-water lake fringe accent |

**Updated temperate riparian palette** (revised after texture verification):
- Primary (60–70%): `dirt`, `coarse_dirt`
- Secondary (20–25%): `podzol` (shaded bends), `rooted_dirt` (under canopy only)
- Accent (≤10%): `mud` (1-pixel scour bands only, Phase 2 test render gates expansion), `clay` (still-water lake fringes only)
- Waterline (1-pixel band): `gravel` permitted but not required

`packed_mud` and broad `gravel` use move to `arid_riparian_fringe` in Phase 5.

Noise profile: reuse legacy mixed-forest noise field (scale + type) to drive palette selection — Nick confirmed the noise math itself was good, only the distribution logic was off. Port the noise function to `core/layers/noise_profiles.py` as `legacy_mixed_forest_noise()`. `temperate_forest_surface` uses the same helper so the two layers share grain at biome boundaries.

**Tree-density coupling (surface block mix ↔ tree density):** Pass 2's `temperate_forest_surface` and `temperate_grass_terrace` both read a `tree_density_hint` field. Because Pass 4 runs *after* Pass 2, this field is **precomputed once at pipeline start** in a new helper `core/tree_density_hint.py` that evaluates the same suitability function Pass 4 will later use for placement — inline, cheap, and tile-scoped. Pass 2 consumes the hint; Pass 4 later uses the same function on its own samples so the two stay consistent by construction. (This is the same cross-pass pattern as vertical fluting's variant hints, just going in the other direction on the pipeline.) Rule:
- **High tree density** (closed canopy hint > 0.6) → forest-floor noise mix dominates (grass_block 50%, coarse_dirt 25%, podzol 15%, rooted_dirt 10%).
- **Low tree density** (open canopy hint < 0.3) → clean grassland (grass_block 90%, coarse_dirt 8%, mossy_cobble 2%).
- **Transition band** linearly interpolates.

This is the loop-close Nick asked for: the forest placement layer in Pass 4 feeds back into Pass 2's surface mix so the ground visibly knows it's "under a forest" vs "under open pasture" even before trees render.

Rough count: 9 layers for MVP pilot, with temperate group + shared. Desert_pavement and boreal_moss_carpet get their own layer files in Phase 5 (horizontal rollout).

**Parallel protocol test (R2 #3 mitigation):** during Phase 2 (temperate pilot), implement ONE desert layer (`desert_pavement`) to prove the Layer Protocol generalizes across biome groups before we lock it in.

**Invariant:** after Pass 2 runs on a temperate-mountain tile, `(prior_ownership != 0).mean() ≥ 0.99` on land pixels. Enforced by unit test on pilot tile.

---

### Pass 3 — Ground Cover

Replaces ground cover logic in `surface_decorator.py`. Each layer emits ground-cover blocks (leaf litter, moss carpet, pale moss, grass variants, bushes, tall grass, ferns, flowers).

**Layers (MVP, ~5):**

| Layer | Type | Scope | Density rule |
|-------|------|-------|--------------|
| `temperate_forest_floor` | partition | temperate forested biomes, grass_block surface, tree_density_hint ≥ 0.3 | `density = base × (0.5 + 0.5 × moisture_idx) × (0.7 + 0.3 × north_factor)`. Mix: `short_grass`, `fern`, `leaf_litter` (1.21.2+), `pale_moss_carpet`, occasional `pink_petals` / `wildflowers`, `bush` (1.21.2+) |
| `temperate_clearing` (formerly `temperate_meadow` — renamed because "meadow" = "clearing" per Nick) | partition | **Noise-driven clearings inside forest and floodplain matrices.** Scope: temperate forest biomes **AND floodplain corridors (gap==4)**, grass_block surface, where the shared `meadow_clearing_field` (single low-freq organic-blob noise, ~200–400 block wavelength, precomputed once and read by both Pass 3 ground cover and Pass 4 tree scatter) drops below threshold. Clearings are emergent gaps in the forest/floodplain fabric — not a painted biome. Floodplain corridors use the same field so natural tree-free stretches along rivers read as grass with short_grass coverage, same as interior forest clearings. | **Dense `short_grass`** across the interior (NOT tall_grass). Flowers are **extremely rare** — sparse single-stem `dandelion` / `poppy` / `oxeye_daisy` only, treated as a barely-there background trace (Poisson rate ~1 per ~500 grass blocks, tunable down to 1/800 if in-world review says still too busy). No `allium` / `cornflower` / `azure_bluet` / `lily_of_the_valley` / `wildflowers` here — those are reserved for the alpine rarity (see `alpine_flower_field`). Clearings read as grass seas, never bouquets. |
| `clearing_edge_dither` (formerly `meadow_forest_edge_dither`) | **overlay** | within ~4 blocks of the clearing↔forest seam (both sides), driven by the `meadow_clearing_field` crossing its threshold. Applies equally to forest clearings and floodplain clearings. | **Surface block dither + vegetation dither.** On the forest side: surface block mix shifts from forest-floor noise toward grass-block, tree schematic density scales down to ~40% of interior. On the clearing side: `tall_grass` band appears (3–4 block wide) as the clearing-side edge signal, with a hair of tree scatter for ~2 blocks deeper into the clearing. Result: visible grass-height dither right at the seam, scarcity band of trees spilling into the clearing. |
| `alpine_grass_meadow` | partition | alpine biomes, grass_block surface | `density = base × (1 - north_factor × 0.5)` (sun-facing denser). Same flower policy as temperate_meadow — trace only. |
| `alpine_flower_field` | partition | **Rare exception.** Alpine grass_meadow pixels where a separate very-low-freq `alpine_flower_rarity` noise field exceeds a high threshold (~top 2–4% of alpine meadow area). Must NOT overlap tree_density_hint ≥ 0.3 and must be sun-facing (`north_factor < 0.4`). | The one place the full flower palette (`poppy` / `oxeye_daisy` / `cornflower` / `allium` / `azure_bluet` / `lily_of_the_valley` / `wildflowers`) comes out at meaningful density. Treated as a prize-tier overlay, not a common biome feature. |
| `riparian_lush` | partition | within 6 blocks of river centerline, temperate | `density = base × 1.5`, fern + short_grass mix (not tall_grass unless near forest edge) |
| `boreal_moss_carpet` | partition | boreal biomes, flat slopes | `density = base × (0.5 + 0.5 × concavity_norm)` |
| `biome_edge_softening` | overlay | within N blocks of biome boundary (see § 9) | Per-biome symmetric edge recipe, each biome owns one rule |

**Inline suitability (R2 #8 mitigation):** no new precompute mask. Each layer computes its density field from existing eco_grads + `moisture_idx` derived inline via `scipy.ndimage.distance_transform_edt` on water masks. If wall-time becomes a problem, promote to precompute later.

---

### Pass 4 — Vegetation / Schematics

Replaces the placement code in current decoration. Schematic recipes (existing config) stay; **placement sampling** changes.

**MVP layers (~4):**

| Layer | Type | Scope | Placement rule |
|-------|------|-------|----------------|
| `temperate_tree_canopy` | partition | temperate biomes, grass_block | Poisson-disk weighted by `suitability = moisture × (1 - steepness) × (1 - disturbance)` |
| `alpine_treeline` | partition | alpine, elevation-band | Density falls off linearly from `treeline_low` to `treeline_high` |
| `disturbance_succession` | partition | windthrow + old floodplain masks | Early-succession recipe — **NO saplings / no growable plants** (see rule below). Replace with tall_grass, fern, large_fern, bush, short_grass, dead_bush where arid, and seasonal flower blocks. |
| `riparian_trees` | partition | within 4 blocks of river centerline | Willow / alder / birch schematic *fully-grown* cluster, denser than upland. Schematics only — no saplings. |

**Poisson-disk weighted by suitability:** standard Poisson-disk with per-candidate rejection probability = `1 - suitability(x, z)`. Gives visible clustering without random holes.

**No-grow rule (VERY IMPORTANT):** no block that Minecraft can tick into growth is allowed as a final placement. This means:
- ❌ No `oak_sapling`, `birch_sapling`, `spruce_sapling`, `jungle_sapling`, `acacia_sapling`, `dark_oak_sapling`, `mangrove_propagule`, `cherry_sapling`, `azalea`, `flowering_azalea`
- ❌ No `wheat`, `carrots`, `potatoes`, `beetroots`, `melon_stem`, `pumpkin_stem`, `sweet_berry_bush` (can grow to stage 3), `cocoa` (stages), `kelp` (grows), `bamboo_sapling`, `sugar_cane` (grows)
- ❌ No `torchflower_crop`, `pitcher_crop` in non-farmable cells
- ✅ Use nearest non-growing equivalents: `short_grass`, `tall_grass`, `fern`, `large_fern`, `bush` (1.21.2+), `dead_bush`, `leaf_litter` (1.21.2+), `pink_petals`, `wildflowers` (if in 1.21.10), plus static flowers (`dandelion`, `poppy`, `azure_bluet`, `allium`, `oxeye_daisy`, `cornflower`, `lily_of_the_valley`, `blue_orchid`), `moss_carpet`, `pale_moss_carpet`.
- Applies to every vegetation layer, not just disturbance_succession. Every block emitted by a Pass 4 layer must pass a `NO_GROW_ALLOWLIST` check before the chunk writer accepts it.

**Block palette accuracy:** Target MC version **1.21.10 Java, DataVersion 4556** (per `CLAUDE.md`). Use known-good blocks only: `bush`, `firefly_bush` (banned in forests per user), `leaf_litter`, `pale_moss_carpet`, `resin_clump` — all 1.21.2+ valid. If I need to emit `bush_block` or similar NBT-sensitive blocks, I use the existing working pattern from `chunk_writer.py` rather than re-deriving it (prior session had `bush_block` generation trouble — don't repeat).

**See Open Question #5:** Nick's latest message referenced "1.20.9 (hotfix 1.20.10)" which conflicts with CLAUDE.md's 1.21.10. Needs resolution before palette is locked.

---

### Pass 5 — Microdetail + World Studio preview cleanup

**Microdetail layers (all overlay):**

| Layer | Scope | Effect |
|-------|-------|--------|
| `water_edge_gradient` | rock/stone pixels within 2 blocks of water surface | **Biome-aware luminosity gradient** — see below. NOT mossy-by-default. |
| `south_face_bleaching` | rock pixels with high solar exposure, by lithology group | Biome-aware gradient toward lighter variants (e.g. calcite near limestone, smooth sandstone near sedimentary, tuff→light variants near deepslate). See gradient principle below. |
| `north_face_moss_targeted` | **only cobblestone pixels** with north_factor > 0.6 in humid biomes | Mossy cobble substitution on cobblestone specifically — not a broad rock overlay. |
| `frost_heave` | cold biomes, flat slopes | Occasional stone/cobble poking through grass |
| `debris_apron_rocks` | below cliff faces, in talus layers | Small boulder scatter (schematic, not block-level) |

**Biome-aware rock gradient principle (Nick's spec):** water edges, bleaching, and other weathering overlays use **block luminosity gradients** matched to the local lithology, not uniform moss application. Examples:

- **Deepslate region water edge:** `deepslate → cobbled_deepslate → tuff → stone → andesite → diorite → calcite` fine-scatter along the waterline, each block picked by distance-from-water + random jitter, creating a dark→light gradient that reads as wet-zone weathering.
- **Granitic / mossy-temperate water edge:** `stone → andesite → diorite → calcite` — andesite is the critical midband (confirmed from the Block gradient reference image: the "light midband" is andesite, not smooth_stone). Deepslate or cobblestone can anchor the dark end depending on basement lithology.
- **Limestone water edge:** `tuff → calcite → dripstone_block → stone` — mineral-deposit feel.
- **Basaltic water edge:** `basalt → blackstone → smooth_basalt → stone → tuff` — volcanic-rubble weathering. (`smooth_basalt` is fine here — the name is misleading but the texture is a cleanly-weathered dark basalt, not concrete.)
- **Sedimentary south-face bleach:** `sandstone → red_sandstone (subtle) → white_terracotta (sparingly) → calcite` — sun-baked highlight. Avoid `smooth_sandstone`; the bevelled edge reads as carved block.

The rule: pick 4–5 blocks from the lithology group (plus close neighbors in the vanilla palette) spanning a clear luminosity range, then fine-scatter by distance-from-waterline or solar-exposure signal. Same logic applies to bleaching, frost-heave highlights, and any other microdetail gradient.

**Moss is a subset, not the default.** Previous design leaned on mossy_cobblestone as the universal "water is here" signal. That's wrong — moss only belongs on specific substrates (cobblestone-heavy pixels in humid biomes), and overusing it makes everything look like a swamp. `north_face_moss_targeted` is the only moss-specific microdetail layer; all other weathering uses the luminosity-gradient principle.

**World Studio preview cleanup:** strip the noise-based default render layers (slope rocks, moisture, etc.) from `tools/world_studio.py` preview. **Keep the base-surface-block palette editor UI** — Nick wants to quickly adjust the per-biome base surface block mix after seeing tiles in-world, and we had a working UI for that. Don't dump it; port it onto the new pipeline's per-biome base surface block config (likely `config/base_surface_palettes.json`). What gets stripped: the noise-based preview render layers (meaningless slope rocks + moisture overlay). What stays: the biome palette editor and the base-block picker per biome.

---

## 7. Phase 0.5 — Agentic Diagnostic Tooling

**Why this exists:** The refactor needs iterative visual evaluation, but I (Claude) can't fly into Minecraft. .mca regen + test-world loading takes ~20 minutes per cycle and requires Nick's input. To iterate autonomously, I need diagnostic renderers that produce reviewable artifacts Nick can spot-check between .mca checkpoints.

**Axes of evaluation: BOTH vertical AND horizontal matter.** Top-down checks catch biome transitions, pixel ownership, ground cover density, river corridors, boundary softening. Cross-sections catch lithology stacks, soil horizon depth, vertical fluting stripe rendering, river-bed geometry, cliff column integrity. Every phase checkpoint produces both axes — neither alone is sufficient.

### Primary tool: `tools/world_viewer.py` (standalone, cache-first)

Inspired by the WorldPainter Custom Object Layer preview panel Nick flagged: an orthographic hillshaded relief viewer with contour overlays, block-ID base coloring, and object markers. It is **not** a real 3D voxel renderer — it is a fast 2.5D tile viewer built on a pyramidal cache so pan/zoom is pure PNG blit, not live compute.

**Explicit: NOT bundled into `tools/world_studio.py`.** World Studio is stale, will break in the refactor, and bolting a new viewer onto it would bloat an already-overloaded tool. `tools/world_viewer.py` ships standalone with its own minimal PyQt6 window, own launch, own cache, own palette editor.

**Architecture — tile pyramid with content-hashed cache:**

```
.viewer_cache/
  {pipeline_hash}/            # hash(pipeline code version + config hash)
    world_overview.png        # single ~1500×1500 image, full world at 1:1024
    region_07/                # 1:128 region overviews
      {rx}_{rz}.png
    tile_1x1/                 # 1:8 per-tile (matches existing precompute scale)
      {tile_x}_{tile_z}.png
    tile_3x3/                 # 1:1 3×3 stitched (1536×1536) — heavy, lazy
      {tile_x}_{tile_z}.png
  input_manifest.json          # mtime + sha256 of every input file
```

**Cache keying:** `hash(git_HEAD + uncommitted_diff(core/layers/*, core/surface_pipeline.py, config/) + input_mtimes)`. Edit a layer → layer caches invalidate, hillshade+contour caches stay valid (they only depend on `height.tif`). Net: typical "I tweaked a threshold" edit triggers a partial rebuild of ~5–15 sec on the active tile, not a full rebuild.

**Rendering layers (composable, user-toggleable):**

1. **Base: block-ID top-down** — surface_y block identity colored via `tools/world_block_map.py` (new canonical `BLOCK_COLORS` LUT, mirrors the `BIOME_COLORS` pattern).
2. **Hillshade** — numpy gradient + Lambertian shading from `height.tif`. Multiplicative blend over base.
3. **Contour lines** — `skimage.measure.find_contours` at configurable elevation intervals, drawn as `QPainterPath` overlay.
4. **Biome overlay** — optional, swaps block colors for biome colors (existing `BIOME_COLORS`).
5. **Layer ownership overlay** — each partition layer colored distinctly. Replaces standalone `diag_layer_ownership.py`.
6. **Suitability field overlay** — per-layer density/suitability as grayscale heatmap. Replaces standalone `diag_suitability_field.py`.
7. **Single mask overlay** — any named mask (`cliff_deg`, `flow_tile`, `aspect`, `concavity_norm`, `north_factor`, `wind_exposure`) as a colormap overlay. Replaces N one-off diag scripts.

**Interaction:**
- Pan/zoom via `QGraphicsView` with pyramid LOD swap. Buttery at any tile count — no live compute on scroll.
- **Left-click-drag a line on the map → cliff cross-section inset** opens in a dock widget. Calls `core/diag/cliff_crosssection.py` (headless function) on the line, renders a Y-slice showing block identity down the column. This is where vertical fluting and lithology stacks get checked without leaving the viewer.
- Shift-click → toggle overlays on/off.
- Right-click tile → "force rebuild" for that tile.
- Status bar: dirty tile count, last rebuild time, cache size.

**Base surface palette editor** (migrated from dead `world_studio.py`):
- Docked panel. Select biome → edit base surface block mix → hit "Apply" → viewer rebuilds the affected tiles. Same workflow as the old World Studio biome palette editor, now wired to `config/base_surface_palettes.json`.
- This is the only piece of `world_studio.py` that survives. Everything else in World Studio is parked/broken.

**Runtime budget:**
- First-load world overview: ~5 sec (single pass over 1:8 masks).
- First-load 1×1 tile: ~8 sec (hillshade + contour + block render).
- First-load 3×3 at 1:1: ~45–60 sec (heavy, lazy on first view).
- Pan/zoom after cache warm: **60 fps** (PNG blit only).
- Rebuild after a layer edit: ~5–15 sec for active tile only; other tiles cached.
- No realtime scroll compute. No GPU required for MVP.

**MVP ships CPU-only.** If 60 fps blit turns out to stutter on the heavy 3×3 view, the fallback is `QOpenGLWidget` + shader hillshade, budgeted at half a day. Not a Phase 0 commitment.

### Secondary tools (headless functions, importable by viewer and runnable standalone)

1. **`core/diag/cliff_crosssection.py`** — *vertical axis*
   - Inputs: tile coords + sample line (2 endpoints). Reads `column_output`.
   - Output: PNG showing vertical slice along the line. Y axis = block Y, X axis = distance along line. Each cell = block identity.
   - Used as: (a) inset panel in `world_viewer.py` click-drag interaction, (b) standalone CLI for baseline snapshots and batch runs.
   - Default sample lines per pilot tile committed to `tests/diag_lines/{tile}.json` for reproducible baselines.

2. **`core/diag/fluting_phase.py`**
   - Computes cliff tangent direction (as hue) and stripe phase (as value) across a tile.
   - Used as: toggleable viewer overlay + standalone PNG emitter for vertical fluting tuning.

**Retired diag scripts** (no longer built as standalones, subsumed into viewer):
- ~~`diag_topdown_blocks.py`~~ → viewer base layer
- ~~`diag_layer_ownership.py`~~ → viewer layer ownership overlay
- ~~`diag_suitability_field.py`~~ → viewer suitability overlay
- ~~`diag_layer_breakdown.py`~~ → viewer "all layers" multi-overlay view
- Existing `diag_layers_breakdown.py` stays as-is during transition; can be deleted after viewer lands.

**Artifact handling:** viewer screenshots + exports write to `diag_output/{tile_x}_{tile_z}/{capture_name}.png`. I (Claude) save them to the workspace folder and present via `mcp__cowork__present_files` at phase checkpoints so Nick can review without opening Minecraft.

**Phase 0 cost impact:** Phase 0 gets ~1.5 days longer to build the viewer, but Phase 2–5 iteration gets tighter because scroll-pan-tweak-rebuild on the active tile is a <30 sec loop instead of 20 min.

---

## 8. Vertical Fluting — Detailed Spec

> ### ⭐ Gold-star feature
>
> **Vertical fluting is the single most impactful visual win this refactor ships.** Cliff faces going from uniform stone tint to real columnar weathering stripes is the change that will read as "this world looks like Norterre" the first time Nick flies past one in-game. Every other pass is table-stakes realism; this is the one that makes cliffs read as *geological*. If a phase budget has to be cut, cut elsewhere before cutting vertical fluting work.

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
- `core/wind_model.py` — single source of truth for tradewind direction (270°, W→E). Exposes `windward_factor(aspect)`, `leeward_factor(aspect)`, `wind_exposure(aspect, slope)`, `fetch_integral(water_mask, origin)`. Every wind-aware layer imports from here.
- `core/tree_density_hint.py` — precomputed tree-density hint field so Pass 2 can read what Pass 4 will emit (without running Pass 4 first).
- `core/meadow_clearing_field.py` — single low-frequency organic-blob noise field (~200–400 block wavelength, one octave of opensimplex at 1:8 precompute res, upscaled bilinear). Precomputed once at pipeline start. **Shared input** consumed by Pass 3 `temperate_clearing` / `clearing_edge_dither` layers AND Pass 4 tree-scatter density weighting, so tree absence and grass clearings line up on the exact same seam. Scope covers temperate forest biomes + floodplain corridors (gap==4). No per-layer noise re-rolling — both passes read the same field for deterministic agreement.
- `core/layers/noise_profiles.py` — contains `legacy_mixed_forest_noise()` ported from the current surface_decorator, reused by `temperate_forest_surface` and `temperate_riparian_fringe`.
- `diag_cliff_crosssection.py`, `diag_topdown_blocks.py`, `diag_layer_ownership.py`, `diag_suitability_field.py`, plus stub for `diag_fluting_phase.py` (minimum four tools ready to emit PNGs).
- Unit tests for partition coverage and overlay behavior on synthetic tiles.
- Unit test: `wind_model.windward_factor(west_facing_aspect) ≈ 1.0`, `leeward_factor(east_facing_aspect) ≈ 1.0`. Catches any sign-flip regression immediately.
- `NO_GROW_ALLOWLIST` constant defined in `core/layers/vegetation_blocks.py`, with a sentinel unit test that fails if any blacklisted block is added to a palette.

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

### ~~Phase 6 — World Studio preview cleanup~~ (DROPPED, S43)

**Dropped per Nick's §11 review.** The new `tools/world_viewer.py` (§7) functionally replaces what this phase was supposed to do. `tools/world_studio.py` will break during the refactor and is considered stale — not worth fixing. The only piece worth saving is the base surface palette editor, which migrates into `world_viewer.py` instead.

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
| R3-1 | Tree-density hint cross-pass dependency (Pass 2 ↔ Pass 4) | M | **Resolved** | Precompute hint once at pipeline start via `core/tree_density_hint.py`; Pass 4 reuses same fn so they stay consistent |
| R3-2 | Tradewind direction sign errors silently invert weathering | H | **Resolved** | Single `core/wind_model.py` owns all wind math; unit test pins W→E direction |
| R3-3 | No-grow rule violations ship silently (saplings in a recipe slip through) | H | **Resolved** | `NO_GROW_ALLOWLIST` enforced at chunk_writer boundary, unit-tested; Phase 5 audit walks every existing schematic recipe |
| R3-4 | MC version ambiguity (1.20.x vs 1.21.x) blocks block palette | H | **Open** | Open Question #5 — blocks Pass 4 palette lock, not Phase 0/0.5/1 |
| R3-5 | `mud` / `rooted_dirt` block textures may not read right at temperate riparian edges | M | **Open** | Diagnostic render before Pass 2 flag flips — Open Question #6 |
| R3-6 | Biome-aware rock gradient palettes are hand-tuned and brittle | M | **Accepted** | Lives in `config/microdetail_gradients.json`; Phase 5 iteration cycles tune per lithology group |

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
9. **Failure logging forks execution — no blind retries.** On any validator regression (a new PASS→FAIL flip against a baseline, or any unexpected failure in a pre-committed check), the agent's execution path must **fork**: stop the current edit chain, write a structured entry to `memory/project_vandir_status.md` (timestamp + what was being attempted + full failure signature + root-cause hypothesis + proposed next diagnostic), and only then decide whether to continue. Retrying the same edit or spraying at the symptom is prohibited. This is the agentic-automation safeguard: because I'm running on autopilot between checkpoints, silent retry loops would waste the entire phase budget on a misread failure. Every failed checkpoint is a stop-and-think, not a stop-and-tweak.
10. **Regression is a first-class signal, not noise.** If a baseline flips from PASS to FAIL on a check unrelated to the current edit, that's a priority-0 investigation — treat it as "something upstream broke, figure out what." Do not mark the check as flaky and move on. Do not update the baseline to suppress the regression.

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

- **108-layer count as a target.** Norterre's 108 is a multi-year aspirational trajectory, not a literal goal. Build layers organically as features demand them. If we get there, we get there. If we ship an excellent pilot with 15 layers, that's the win.
- **Two-pass heightmap in World Machine style.** Vandir's Gaea heightmap is fixed and single-pass. Work around it rather than re-importing. Confidence is high that the mask-based Pass 0–5 approach covers the realism delta without needing a second heightmap pass.
- **Per-adjacency-pair boundary rules.** *Explained for clarity:* the bad version was writing transition recipes for every PAIR of neighboring biomes (desert↔forest, forest↔alpine, etc.) — O(N²), up to ~190 rules for 20 biomes, brittle. The good version (which we ARE shipping, § 9) is one symmetric edge rule per biome: each biome owns "what I do at my own edge, regardless of who's on the other side." 20 biomes → 20 rules. Adding a biome = 1 new rule, not 19.
- **Wind / weather simulation.** Tradewind direction is a static 270° constant (§ 3 principle #9). Dynamic weather is not in scope.
- **Seasonal variation** (snowfall changes, leaf-drop cycles, seasonal grass color). **Dropped entirely per Nick.** Doesn't make sense for a static-world pipeline.
- **Stratification rings bug as a standalone fix.** Subsumed by vertical fluting (§ 10). Goes away automatically when the `_apply_desert_rock_palette` stratification step is deleted.
- **`chk_no_bare_dirt_surface` validator false positive on `51_53`.** Locked as known-state in baselines.
- **Schematic index Y-offset sink bug.** Intermittent issue where some schematics sink below surface due to default placement Y offset. Fix later (Phase 5+ or post-pilot). Not blocking the refactor.
- **`tools/world_studio.py` repair.** Stale, will break during refactor, not worth fixing. Only the base surface palette editor survives, migrated to `tools/world_viewer.py`.

### 🚫 NO STRUCTURES — hard rule, not a parking-lot item

There are **no player-made structures** in Vandir, ever, in any form:

- **No buildings.** No houses, cabins, towers, watchtowers, huts, shelters.
- **No villages.** No village gen, no village schematics, no abandoned-village schematics.
- **No ruins.** No ruined portals, no ruin structures, no stone-brick ruins.
- **No dungeons / strongholds / outposts / mansions / monuments / temples / mineshafts / end-portals.**
- **No lore-implied artifacts.** No mystery chests, no signs, no carved stone, no suspicious gravel, no crafted items placed in-world.
- **No animals as placed entities.** (Vanilla MC will spawn mobs from biome rules — that's fine and out of our control. But we do not place mob schematics.)

**What IS allowed** (the only things Pass 4 emits): grown tree schematics, rock schematics (boulders, talus scatter), debris schematics (fallen logs, root piles), plant schematics (bushes, ferns, flowers as entities if needed). Everything here is something that would exist without a human in the world.

**Enforcement:** Phase 5 includes a schematic-index audit. Every entry in `schematic_index.json` is classified as `natural` or `structure`. Any `structure` entry is removed from the placement sampler — existing recipes stay in the file as dead code but cannot be called. A unit test in Phase 0 pins that no layer in `core/layers/pass4_vegetation/` imports any structure-classified schematic ID.

I have been told this enough times that getting it wrong again is a capital offense. Reminder logged.

---

## 16. Open Questions

### Resolved (S43, 2026-04-10)

1. **Cave carving?** **No.** Everything below surface is solid. R1-5 (cave-carver interaction) collapses. Subsurface lithology pass owns the full column below the soil horizon with no carver coordination needed.
2. **`wave_fetch.tif` at 1:8?** **In scope.** Directional distance transform over water at 6250×6250 is cheap (~seconds with scipy). Claude will precompute it in Phase 0.5 alongside lithology. Used by coastal beach width in Pass 2+. Tradewind direction (270° W→E) biases the fetch integration — west-facing coasts get long fetch, east-facing coasts get short fetch.
3. **Horizontal rollout pace?** **Autopilot.** Once pilot tile 36_20 passes acceptance, Claude drives biome-group expansion at its own cadence, logging each group completion to `memory/project_vandir_status.md`. Nick vetoes on review.
4. **Schematic index + recipes?** **Unchanged for this refactor.** Index and recipe files remain exactly as-is during the refactor. Only the placement *sampler* changes (algorithmic → suitability-weighted Poisson-disk from inline vegetation suitability fields driven by `elevation_sparseness` and `moisture_sparseness`). Saplings in current schematic recipes get audited against the `NO_GROW_ALLOWLIST`; any sapling-based recipes get swapped to equivalent grown-tree schematics. **Known bug (out of scope):** some schematics intermittently sink below surface due to default placement Y offset. Fix later, not blocking.
5. **Minecraft version?** **1.21.10 Java, DataVersion 4556.** Nick's earlier "1.20.10" was a typo. Verified against `meta/versions/1.21.10/1.21.10.jar` in the ModrinthApp mount. `bush`, `firefly_bush`, `leaf_litter`, `pale_moss_carpet`, `resin_clump` are all valid 1.21.2+ blocks and cleared for use. Pass 4 palette and `NO_GROW_ALLOWLIST` can lock. **R3-4 closes.**
6. **Riparian palette block-texture verification.** Resolved via direct read of block textures from the 1.21.10 jar on 2026-04-10. See the verification table in § 6 Pass 2. `packed_mud` and broad `gravel` moved out of temperate riparian. `mud` kept as narrow accent pending a Phase 0.5 test render. `rooted_dirt` kept with a `near_canopy_hint` predicate.

---

## 17. Signoff

- [x] Nick: Plan approved to begin Phase 0 (S43, 2026-04-10).
- [x] Nick: Open questions 1–4 answered (S43, 2026-04-10).
- [ ] Claude: Phase 0 baseline + diagnostic tool skeleton ready to commit on Phase 0 kickoff.

---

## 18. Implementation Log

**This section is the source of truth for what has actually been built vs. the design above. Every Phase lands here with the date, the files it touched, the tests it added, and any open bugs. Future sessions read this first to catch up.**

### S44 — Phase 0 + 0.5 (2026-04-11)

**Status:** Phase 0 signed off. Phase 0.5 (lithology + wave_fetch + texture sanity + world_viewer MVP) landed in the same commit per user direction ("roll 0 and 0.5 into one commit and documentation moment").

#### Phase 0 — Scaffolding

**Goal:** Get the layer protocol, pipeline runner, wind model, field helpers, and no-grow guardrails in place so Phase 2+ pilot layers can land against a stable API.

**New files:**
- `core/layers/__init__.py` — package marker.
- `core/layers/protocol.py` — `SurfaceContext`, `LayerResult`, `Layer` Protocol (id, pass_num, priority, kind, apply). `empty_result()` / `make_result()` helpers with shape+dtype validation. `EMPTY_BLOCK = ""` sentinel.
- `core/layers/noise_profiles.py` — `legacy_mixed_forest_noise()` port of `_noise_tile()` from `surface_decorator.py`. 3-octave fBm via `opensimplex.noise2array`, persistence=0.5, lacunarity=2.0. Defaults: `LEGACY_MIXED_FOREST_SCALE = 60.0` (matches `config/thresholds.json → block_mixing.noise_scale`), `LEGACY_MIXED_FOREST_SEED = 42002`. Pure-numpy fallback when `opensimplex` unavailable.
- `core/layers/vegetation_blocks.py` — `NO_GROW_BLOCKLIST` (saplings, crops, growable shrubs) + `NO_GROW_ALLOWLIST` (static grasses/flowers + 1.21.2+ blocks: `bush`, `leaf_litter`, `pale_moss_carpet`, `firefly_bush`, `resin_clump`). `validate_no_grow()` + `assert_palette_safe()` enforce at layer boundaries.
- `core/surface_pipeline.py` — `run_pass(layers, ctx, strict=True)` with partition (writes only where `ownership == 0`) and overlay (bitmask at `idx % 8`) composition semantics. `run_passes()` for multi-pass chains. `partition_coverage()` helper. `_validate_result()` enforces shape + kind match + `EMPTY_BLOCK` invariant on partition layers.
- `core/wind_model.py` — Single-source-of-truth tradewind module. `WIND_FLOW_VECTOR = (1.0, 0.0)` (east-pointing). `WIND_SOURCE_HEADING_DEG = 270.0`. `windward_factor(aspect) = (1 - cos(aspect))/2` (west-facing = 1). `leeward_factor`, `wind_exposure`, `fetch_integral` (vectorized row-wise cumsum, O(N)). Constants: `WEST/EAST/NORTH/SOUTH_FACING_ASPECT`.
- `core/meadow_clearing_field.py` — `compute_meadow_clearing_field()` + `clearing_interior_mask()` / `clearing_seam_mask()`. opensimplex-backed at a fixed world wavelength.
- `core/tree_density_hint.py` — `TEMPERATE_FORESTED_BIOMES` constant + `compute_tree_density_hint(biome_grid, slope, moisture_idx, disturbance)` → float32 [0,1]. Placeholder kernel `0.35 + 0.55*moisture - 0.45*slope` for temperate forested biomes, 0 elsewhere. Will be wired to real drivers in Phase 3.

**Diagnostic stubs** (root dir, each callable standalone, writes PNG to `diag_output/{tx}_{tz}/`):
- `diag_cliff_crosssection.py` — synthetic column stack PNG.
- `diag_topdown_blocks.py` — checker pattern PNG.
- `diag_layer_ownership.py` — radial bands with 5% unclaimed fraction.
- `diag_suitability_field.py` — Gaussian blob + noise. Bug fix: `seed = (abs(hash(layer)) ^ (tile_x*1009 + tile_z)) & 0xFFFFFFFF` (was failing with negative int from `hash()`).
- `diag_fluting_phase.py` — HSV tangent/phase rendering for vertical fluting hints.

**Tests** (all green, 26 passing):
- `tests/unit/__init__.py`
- `tests/unit/test_surface_pipeline.py` — empty-list zero output, partition exclusivity, overlay preservation, invariant rejection.
- `tests/unit/test_wind_model.py` — **sign pins**: west=windward (factor=1 at aspect=π), east=leeward (factor=1 at aspect=0), N/S neutral, windward+leeward=1, wind_exposure scales with slope, fetch direction (land east-of-water sees fetch=8, land west-of-water sees fetch=0), vector constants = (1,0) and heading = 270°. This test is the canary — a single sign flip here runs the whole weathering story backwards.
- `tests/unit/test_vegetation_blocks.py` — allowlist/blocklist disjoint, `validate_no_grow()` rejects `oak_sapling`, `assert_palette_safe()` catches `wheat`.
- `tests/unit/test_phase0_smoke.py` — import sanity, field shape/range, `tree_density_hint` kernel behavior (zero in desert, nonzero in moisture-rich forest, suppressed on slope).

**Resolved Q&A from §16:**
1. **Cave carver?** Grep over `core/` for `cave|carver` returned only "concave"/"concavity" substring matches. R1-5 collapses — Vandir has no cave carver, so the subsurface pass doesn't need to coordinate with one.
2. **`wave_fetch` at 1:8?** Confirmed: vectorized cumsum in `wind_model.fetch_integral` handles the full 6250×6250 water mask in sub-second time. Precomputed in Phase 0.5 below.
3. **noise_scale default?** Read `config/thresholds.json → block_mixing.noise_scale` directly: value is `60`. `legacy_mixed_forest_noise()` default updated from initial guess of 48.0 → 60.0 to match.

#### Phase 0.5 — Precomputes + tooling

**Goal:** Land the precomputed masks and diagnostics that Phase 2 pilot layers need, plus the world viewer that lets Nick review output across the whole 50k×50k map without firing MCA cycles.

**Config changes** — `config/thresholds.json`:
- Added top-level `lithology` key (total keys now 30). Structure:
  - `feature_flag_enabled: false` — all lithology-aware code paths must honor this gate. Default OFF to de-risk Phase 0.5.
  - `groups`: 6 entries (`granitic`, `sedimentary`, `basaltic`, `limestone`, `deepslate_metamorphic`, `mossy_temperate`), each with `id` (1..6) + 4-block `palette` + description.
  - `zone_to_group`: 27-entry LUT mapping every real `OVERRIDE_BIOME_MAP` zone name to a group. (Draft LUT in §6 Pass 0 used some zone names that don't exist — remapped to the real zones from `core/biome_assignment.py`.)
  - `elevation_overrides.rules: []` — structure wired, no rules yet. Phase 2+ adds alpine → deepslate_metamorphic etc.
  - `basement`: `shallow_depth_blocks: 12`, `shallow_group_from_surface: true`, `deep_default_group: "granitic"`.

**New tools:**
- `tools/build_lithology.py` — produces `masks/lithology.tif` (6250×6250 uint8, 1..6, 0 = water/unclassified). Reads `override.tif` via windowed row-by-row read to avoid holding 2×2.5GB of uint8 in RAM (OOM-killed on first attempt with naive `src.read(1)`). Applies `elevation_overrides.rules` on demand. **Distribution on real masks:** 60.1% zero (water+unclassified), 12.9% granitic, 12.0% sedimentary, 1.1% basaltic, 1.2% limestone, 7.2% deepslate_metamorphic, 5.5% mossy_temperate.
- `tools/build_wave_fetch.py` — produces `masks/wave_fetch.tif` (6250×6250 uint16, capped at `--max-distance` px). Prefers `shore.tif` for water mask, falls back to `height.tif < sea_level_16bit`. Uses `wind_model.fetch_integral()`. **Runtime:** ~1s on the 6250×6250 grid. **Result on real masks:** 19149 nonzero shore pixels (0.049% of grid), mean nonzero = 92.1, max = 128 (cap hit). Only 0.05% of pixels are "land directly east of water" — matches expectation, the shoreline is ~1 precompute cell wide.
- `tools/extract_riparian_textures.py` — reads `/sessions/.../ModrinthApp/meta/versions/1.21.10/1.21.10.jar` and writes 8 block textures (dirt, coarse_dirt, rooted_dirt, podzol_top, mud, gravel, clay, packed_mud) to `diag_output/riparian/*.png` + a composed sanity swatch `_palette_sanity.png`. Closes the R3-4 verification loop visually before Phase 2 tuning.
- `tools/world_viewer.py` — **MVP**, blind-written (no PyQt6 in sandbox). Splits into:
  - Headless data layer (importable from tests): `LayerSpec`, `TileCache` (LRU over `(layer, zoom)` full-raster downsamples at factors 1/2/4/8), `discover_layers()`, `compute_hillshade()` (Horn method, USGS convention), `extract_cliff_section()` (straight-line transect by compass azimuth), `apply_colormap()` with inline `viridis`/`magma`/`terrain` LUTs (no matplotlib dep).
  - Qt layer (gated by `try: import PyQt6`): `WorldCanvas` (QGraphicsView with wheel-zoom 0..3 and pan drag), `LayerPanel` (checkboxes auto-populated from `discover_layers()`), `CliffInset` (256×128 QLabel rendering). Bootstraps base height from `masks/height.tif` via windowed read + `_downsample_mean(factor=8)`. Imports `PaletteEditorWidget` from `tools/world_studio.py` if available; degrades gracefully if not.
  - Headless smoke test passed: 24 layers discovered (including new `lithology` + `wave_fetch`), hillshade/cliff/colormap/downsample helpers all green against synthetic inputs.

**Validator extensions** — `tools/validate_masks.py`:
- Added `lithology` bound (`discrete`, 0.25 ≤ cov ≤ 0.45).
- Added `wave_fetch` bound (`gradient`, 0.0 ≤ cov ≤ 0.02 — shore-only signal is sparse).
- Added `check_lithology_extras()` running 4 extra sanity checks: (1) valid IDs only (must be subset of {0..6}), (2) at least 4 of 6 nonzero groups present, (3) zero-fraction in [0.40, 0.80] (must align with land/water), (4) shape = (6250, 6250). **All 6 checks pass on `masks/lithology.tif` + `masks/wave_fetch.tif`:**

  ```
  ✅ lithology: discrete cov=0.399
  ✅ wave_fetch: gradient cov=0.000
  ✅ lithology_ids: all ids in [0..6]
  ✅ lithology_group_diversity: 6 nonzero groups present
  ✅ lithology_water_alignment: zero_frac=0.601 in [0.40, 0.80]
  ✅ lithology_shape: 6250×6250 (1:8)
  ```

**Open bugs:**
- Aspect convention drift: `core/eco_gradients.py` line 132 computes `aspect = np.arctan2(gy, gx)` and comments it as "direction of steepest DESCENT" (mathematically backwards; it's the gradient direction, not descent). The sign of `gy` in `eco_gradients` is inverted relative to `wind_model` convention. **Action for Phase 2:** layers that need aspect should recompute from gradient using the `wind_model` sign convention, not trust `eco_gradients.aspect`. Pinned by test_wind_model.py — a single sign flip rolls the weathering backwards.
- `world_viewer.py` is blind-written. Expect runtime fixes on first Qt import (PyQt6 API surface drift, QGraphicsView zoom math). Data layer tested green; GUI path is the risk.

**Commit hygiene:**
- Phase 0 + 0.5 rolled into a single commit per user direction.
- **Push is blocked in sandbox** — Nick pushes from his terminal.

**Next up (Phase 1 / Phase 2):**
- Phase 1: wire `core/surface_pipeline.run_passes()` into `core/surface_decorator.decorate_surface()` as an additive call (shadow mode — new pipeline runs but output ignored). Confirms zero-impact path exists before Phase 2 hands a pilot layer real ownership.
- Phase 2 pilot: temperate_forest_surface → temperate_riparian_fringe → temperate_windthrow_surface on tile (36, 20). Drives `--baseline tests/baselines/3x3/36_20` (need to snapshot that baseline before Phase 2 starts).
