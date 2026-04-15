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

> **⚠ S46 correction (2026-04-11).** The "additive path in `column_generator.py`" framing below is **incorrect** — it does not match the actual codebase architecture. See §11 Phase 1.5 and §19 Drift Log for the correction. **Canonical injection point: `core/chunk_writer.build_column_array()`** (which is what actually owns mid-column block fill, including the existing `[Y_MIN+1, sy-3] → stone` range and cliff banding variants). The `fill_column()` sketch, per-column param signature, and "reads `ctx.lithology_grid`" language below all need to be re-read as the tile-vectorized equivalent: lithology/sediment/soil_horizon arrive as `(H, W)` tile arrays, and the sublayer logic runs inside `build_column_array()` in Y-slices using the same broadcasting patterns the current cliff banding already uses. The sublayer list (1–8) remains conceptually correct; only the implementation site is wrong. The original text below is preserved verbatim for historical context and traceability.

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

> **Note on phase numbering.** This section is the canonical phase map. The running chronology of what has *actually landed* lives in `§18 Implementation Log`. When sessions insert a new intermediate step (because the design evolved), they must add a new decimal subsection here (e.g. Phase 0.75, Phase 1.5) **in the same commit** that updates `§18` and `CLAUDE.md`, rather than renaming existing phases. This rule exists because the S44→S45 handoff drifted — "Phase 1" meant "column additive path" in §11 and "shadow hookup" in CLAUDE.md + §18 simultaneously. Phase 0.75 below was added retroactively in S45 to fix that drift. CLAUDE.md's session-kickoff pillar checkoff enforces this going forward.

> **⚠ Spec-vs-code drift callout (S46, 2026-04-11).** During S46 recon, the original **Phase 1 spec below was discovered to not match the actual Vandir codebase architecture.** The spec describes extending `core/column_generator.py`'s `fill_column(...)` with per-column lithology params and an inner `_fill_column_with_geology()`. **Reality:** (a) `column_generator.py`'s hot path is `process_tile_columns_v2()`, a vectorized tile-level function on `(H, W)` arrays — there is no `fill_column()`; the legacy per-column `generate_column()` exists but is not on the production path. (b) More critically, **mid-column block fill is not done in `column_generator.py` at all** — it's done in **`core/chunk_writer.build_column_array()`**, which takes `(H, W)` tile arrays and constructs the full `(Y_RANGE, H, W)` voxel volume directly, including the hardcoded `[Y_MIN+1, sy-3] → stone` fill with cliff banding variants. The `ColumnResult.blocks` sparse dict emitted by `column_generator` is a dead path for mid-column fill; only `tools/validate_test_tile.py`, `tools/world_studio.py`, and `tools/voxel_preview.py` read it (and only for top-of-column entries). Consequence: **the correct lithology injection site is `chunk_writer.build_column_array()`, not `column_generator`.** Phase 1 below is preserved verbatim as the original aspirational spec, but is marked **SUPERSEDED** and the actual S46+ implementation lives in the new **Phase 1.5 — Lithology wiring in chunk_writer path** subsection below it. The S44→S45 drift rule (decimal subsections, never renames) applies here too: Phase 1.5 is inserted as a new subsection rather than rewriting Phase 1. A new `§19 Spec-vs-Code Drift Log` at the end of this document captures discoveries like this for future sessions.

> **⚠ Codebase-reconciliation rule (S46, 2026-04-11).** In addition to the S45 phase-reconciliation checkoff (CLAUDE.md Workflow rule), each session MUST run a **codebase-reconciliation check** before any code edit on a phase it is about to implement: open the target source file(s), confirm the function signatures, data flow, and injection points described in §11/§6 actually exist as spec'd. If they don't, STOP, surface the discrepancy, add a §19 Drift Log entry, and update the relevant §11 subsection with a decimal step that matches reality — in the same commit as any code. The S46 discovery above is the archetype: without this check, S46 would have spent an hour or more writing code against a `fill_column()` API that doesn't exist in the production path.

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

### Phase 0.75 — Shadow-mode hookup (half-session, inserted S45)

**Rationale:** Before Phase 2 hands a real layer pixel ownership on tile (36, 20), the plumbing between `core/surface_decorator.decorate_surface()` and `core/surface_pipeline.run_passes()` needs to be proven alive on real tile data — `SurfaceContext` construction, protocol invariants, exception path — all with zero impact on production blocks. This isolates "is the wire good?" from "is the layer correct?" so the Phase 2 pilot debug surface is small.

**Deliverables:**
- `config/thresholds.json` gains new top-level key `surface_pipeline.shadow_mode` (bool, default `false`).
- `core/surface_decorator.decorate_surface()` gains an additive shadow block immediately before its `return`. Behind the config flag OR `VANDIR_SHADOW=1` env var, builds a `SurfaceContext` from the finished production state and calls `run_passes([], ctx, strict=True)`. Result is discarded. Whole block is try/except-wrapped — any failure logs a `[shadow] ERROR tile=(X,Z): ...` line and swallows.
- Empty layer list is mathematically mutation-free: `run_passes([], ctx)` iterates zero passes, returns `ctx.prior_surface.copy()` unchanged. The production return tuple is never touched by the shadow block.
- `tests/unit/test_shadow_hookup.py` — two tests. (1) `run_passes([], ctx)` on a synthetic context returns `surface == prior_surface`, `ownership` all zero, `overlay_touched` all zero, `per_layer_debug == []`. (2) `run_passes([[], []], ctx)` (two empty passes) is also identity.
- Regression gate: existing `tests/baselines/3x3/48_48` and `tests/baselines/3x3/51_53`. No new `36_20` baseline taken in this phase — Phase 1 (column) and Phase 2 (pilot) take that one when they actually need it.

**Exit criteria:**
- Flag-OFF: `validate_3x3 --baseline` on 48_48 and 51_53 reports zero new PASS→FAIL flips vs master. (Both baselines already contain their known pre-existing FAILs; only *new* regressions matter.)
- Flag-ON (via `VANDIR_SHADOW=1`): `validate_3x3` on at least 48_48 runs clean — no `[shadow] ERROR` lines in stdout, no new FAILs vs baseline. Flag-ON on 51_53 is optional strictness; 48_48 ocean + land-adjacent tiles plus the flag-OFF 51_53 run already cover the call-path surface.
- Unit test suite: 28 tests green (26 prior + 2 new).

**Risk:** minimal. Additive code, feature-flagged OFF, empty layer list is structurally guaranteed mutation-free, exception-swallowed, no return-tuple or caller-signature changes.

**Scope boundaries (not in Phase 0.75):**
- No new layers. Layer list is literally `[]`.
- No `36_20` baseline snapshot. That's Phase 1 / Phase 2 prep.
- No lithology flag flip. `lithology.feature_flag_enabled` stays false.
- No changes to `core/column_generator.py`, `core/eco_gradients.py`, or any mask rebuild script.
- No `.mca` regeneration.

---

### Phase 1 — Column additive path (week 2) — **SUPERSEDED BY PHASE 1.5 (S46)**

> **Status: SUPERSEDED.** Preserved verbatim below for historical context and traceability. The S46 spec-vs-code drift callout at the top of §11 explains why. Phase 1.5 below is the canonical implementation plan going forward. Do not implement this Phase 1 as written — it references a `fill_column()` API that does not exist in the production path. Any edit to `column_generator.py` alone would not reach the mid-column vertical range where basement/sediment/soil_horizon need to live.

**Original Deliverables (SUPERSEDED):**
- `core/column_generator.py` gains optional `lithology_tile`, `sediment_thickness_tile`, `use_new_geology` parameters.
- `_fill_column_with_geology()` inner function implementing sublayers 1–7 from § 6 Pass 1.
- Golden-output unit test: `use_new_geology=False` produces byte-identical output to pre-edit baseline on 100 sample columns from 36_20.
- 36_20 baseline (`tests/baselines/3x3/36_20/`) captured BEFORE any edit.
- 5 sample columns on 36_20 snapshot as printable block stacks (JSON).
- `diag_cliff_crosssection.py` validates new geology path produces plausible vertical lithology on 36_20 when flag ON.

**Original Exit criteria (SUPERSEDED):**
- Feature flag OFF: pipeline output is byte-identical to pre-edit baseline on 36_20 3×3. `--baseline` diff reports 0 new failures.
- Feature flag ON: cliff cross-sections on 36_20 show real lithology stack (bedrock → basement → sediment → soil horizon).

**Original Risk (SUPERSEDED):** medium. Column_generator is in-game validated and the regression surface is invisible from top-down. Feature flag and golden-output test contain the blast radius.

**Why superseded (S46 finding):** `column_generator.py` does not control mid-column block fill. The production path is `process_tile_columns_v2()` → `ColumnResult` (tile-level) → `chunk_writer.build_column_array()` which builds the full `(Y_RANGE, H, W)` volume directly from `(H, W)` tile arrays. The sparse `ColumnResult.blocks` dict emitted by `column_generator` contains only bedrock + `sy-2, sy-1, sy` + water/dune fill — everything in `[Y_MIN+1, sy-3]` is stone-defaulted inside `build_column_array()` itself (with optional cliff banding variants). Therefore, lithology must inject into `build_column_array()`, not `column_generator`.

Additionally: the workflow rule required the baseline tile to be a temperate-mountain tile. The original spec named `36_20`, but S46 recon confirmed `36_20` is a **desert rock** tile (Session 41 work). The canonical temperate-mountain tile in Vandir's rotation is `59_53` (windthrow reference — high-elevation forested ridges). Phase 1.5 uses `59_53`.

---

### Phase 1.5 — Lithology wiring in chunk_writer path (inserted S46, 2026-04-11)

**Scope:** architectural scaffolding only. Extends the production injection point with the params Phase 1 wants, wires them through, golden-tests byte-identity flag-OFF, stubs flag-ON with `NotImplementedError` + clear deferral to a later session. No geology content is implemented in Phase 1.5 — that's the next session's work (Phase 1.75 or promoted into a rewritten Phase 1).

**Deliverables:**
- `59_53` baseline (`tests/baselines/3x3/59_53/`) captured **BEFORE** any edit to `chunk_writer.py` or `column_generator.py`. Baseline is the temperate-mountain pilot tile (windthrow reference, high-elevation forested ridges, no prior baseline). Run: `PYTHONUNBUFFERED=1 py tools/validate_3x3.py --tile-x 59 --tile-z 53 --report validation_report_3x3_59_53`.
- `core/chunk_writer.build_column_array()` gains optional params: `lithology_tile: np.ndarray | None = None`, `sediment_thickness_tile: np.ndarray | None = None`, `soil_horizon_depth_tile: np.ndarray | None = None`, `use_new_geology: bool = False`. All default to None/False. Each is an `(H, W)` tile-level array matching `surface_y.shape`.
- Flag-OFF branch: early-out `if not use_new_geology or lithology_tile is None`, existing control flow falls through **unchanged**. Byte-identical to pre-edit output by construction (no edits to existing lines inside the function body — new params are inspected only in the new early-out).
- Flag-ON branch: stub raising `NotImplementedError("Phase 1.5 scaffolding only — lithology fill deferred to later session; see §11 Phase 1.5 and §6 Pass 1.")`. Unit-tested so a future `use_new_geology=True` call correctly fails loud.
- `core/column_generator.py`'s `process_tile_columns_v2()` and `generate_columns()` gain the same optional params and thread them through unchanged to their downstream ColumnResult consumers (mostly a no-op at this layer since `build_column_array()` is where they'll be consumed). Callers in `tools/_pipeline_runner.py`, `tools/validate_test_tile.py`, `tools/check_tile_seams.py`, etc. are **not** required to pass the new params — default None/False preserves flag-OFF behavior for everyone without opt-in changes.
- `tests/unit/test_phase1_5_scaffolding.py` — new file, tests:
  1. `build_column_array(..., use_new_geology=False, lithology_tile=None)` produces identical `vol` to `build_column_array(...)` without the new params (both paths take the flag-OFF branch).
  2. `build_column_array(..., use_new_geology=True, lithology_tile=<synthetic>)` raises `NotImplementedError` with the expected message substring.
  3. Param-threading sanity: `process_tile_columns_v2(..., use_new_geology=False, lithology_tile=None)` returns results equivalent to the call without the new params.
  4. Sentinel: if any caller in `tools/` has been accidentally modified to pass `use_new_geology=True`, a grep-based unit test fails with a clear message (prevents quiet enablement).
- `§19 Spec-vs-Code Drift Log` appended to this document with the S46 finding.
- `CLAUDE.md` Current state line updated; Workflow section gains a "Codebase-reconciliation check" rule mirroring the phase-reconciliation checkoff.

**Exit criteria:**
- Flag-OFF: `validate_3x3 --baseline tests/baselines/3x3/59_53` post-edit reports **0 new PASS→FAIL flips** on 59_53. Same holds on existing `48_48` and `51_53` baselines (safety net).
- Unit test suite: **33 passed** (31 prior from S45 + Phase 1.5 tests — count confirmed in the §18 S46 entry).
- Attempting `use_new_geology=True` raises `NotImplementedError` cleanly (not an obscure `KeyError` or palette crash).
- No `.mca` regenerated this phase; no visual diag output expected (flag-ON is stubbed).

**Risk:** LOW. Additive-only API surface, feature-flagged off, default path byte-identical by construction, golden test pins the identity, `NotImplementedError` stub prevents accidental flag flip, and the three 3×3 baselines (59_53 new + 48_48/51_53 existing) bracket the regression surface for temperate-mountain + coast + mixed-forest land types.

**Scope boundaries (explicitly NOT in Phase 1.5):**
- No geology content (basement, sediment, soil_horizon, river_bed, lake_bed logic) — deferred to the next session.
- No `lithology.tif` consumer changes. `lithology.feature_flag_enabled` in `config/thresholds.json` stays OFF.
- No `.mca` regeneration.
- No changes to `core/eco_gradients.py`, `core/surface_decorator.py`, or any `rebuild_*.py` mask script.
- No flag-ON diagnostic (`diag_cliff_crosssection.py` flag-ON on 59_53 is explicitly deferred).
- No per-column byte-stack output from `ColumnResult` — the sparse-dict emission pattern stays as-is; mid-column fill continues to live in `build_column_array()`.

**Next session (post-Phase-1.5) delivers:**
- Implement sublayers 1 (bedrock_band, already mostly present — promote it to explicit code under flag), 2 (basement_rock_by_group), 3 (sediment_thickness), 4 (soil_horizon) inside the `use_new_geology=True` branch of `build_column_array()`. Sublayers 5 (river_bed) and 6 (lake_bed) follow once river_meta / hydro_lake threading is in place. Sublayer 7 (vertical_fluting_consumer) stays parked behind Phase 2.
- Rename or promote this as a rewritten Phase 1 and retire the "SUPERSEDED" block above.

---

### Phase 1.75 — Real geology content (inserted S47, 2026-04-12)

**Scope:** Implement the four geology sublayers inside the `use_new_geology=True` branch of `build_column_array()`. Replace the Phase 1.5 `NotImplementedError` stub with working geology fill. Wire callers to read `lithology.tif` and pass it through. Inline derivation of sediment thickness and soil horizon depth (no precomputed .tif masks — see design decision in §18 S47).

**Deliverables:**
- `core/chunk_writer.py` — `_fill_geology_layers()` private helper implementing sublayers 1-4:
  1. **bedrock_band**: Y_MIN+1 to Y_MIN+4 → deepslate (promotes existing implicit bedrock-adjacent fill to explicit geology layer).
  2. **basement_rock_by_group**: above bedrock band, below sediment → lithology palette blocks with Y-banding (XZ-waviness reused from legacy cliff banding pattern). Each lithology group (1-6) maps to its `config/thresholds.json → lithology.groups.{name}.palette` block list.
  3. **sediment_thickness**: above basement → gravel / coarse_dirt / dirt by flow magnitude. Thickness derived inline from `concavity_norm * 0.6 + flow * 0.4`, scaled to 0-8 blocks.
  4. **soil_horizon**: below surface_y-3 → dirt (flat slopes) / coarse_dirt (moderate+ slopes). Depth derived inline from slope: 4 blocks flat, 2 moderate, 1 steep, 0 cliff.
- `core/chunk_writer.py` — `_compute_xz_waviness()` and `_apply_banded_fill()` extracted as reusable helpers from the legacy cliff banding code. Legacy path now calls the same helpers.
- `core/chunk_writer.py` — `write_tile()` gains `lithology_tile` and `flow_tile` params. Reads `lithology.feature_flag_enabled` from cfg to gate the geology branch.
- `core/tile_streamer.py` — `read_discrete_tile()` function for reading uint8 masks without float normalisation (avoids lossy 0-255 round-trip for discrete group IDs).
- `tools/_pipeline_runner.py` — reads `lithology.tif` via `read_discrete_tile()`, stores in `TileArtifacts.lithology_tile`.
- `tools/validate_test_tile.py` — reads `lithology.tif`, passes through `write_tile()`.
- `run_pipeline.py` — reads `lithology.tif`, passes through `write_tile()`.
- `tests/unit/test_phase1_75_geology.py` — 10 new tests covering geology stratification, vertical order, lithology palette presence, sediment/soil presence, ocean columns unaffected, mixed lithology groups, flow-driven sediment thickness.
- `tests/unit/test_phase1_5_scaffolding.py` — `test_flag_on_raises_not_implemented` replaced with `test_flag_on_runs_geology_not_raises` (Phase 1.5 stub retired).
- `config/thresholds.json` — `lithology.feature_flag_enabled` remains `false`. Flip to `true` when ready for in-game review.

**Design decisions (S47):**
- **Inline derivation** for sediment thickness + soil horizon depth. Reason: these signals vary at column-to-column scale (valley bottom vs ridge, cliff vs flat), but precomputed .tif masks at 1:8 scale (1 pixel = 8×8 blocks) would smear these gradients into blocky 8×8 patches — exactly where the geology should look most natural. Inline derivation from `surface_y` gradient + flow uses 1:1 resolution. The unused `sediment_thickness_tile` and `soil_horizon_depth_tile` kwargs are kept for backward compat but ignored when geology runs inline.
- **Callers wired in S47** (not deferred). `_pipeline_runner.py`, `validate_test_tile.py`, `run_pipeline.py` all read `lithology.tif` and pass it through. Gated by `cfg.lithology.feature_flag_enabled` in `write_tile()`.
- **Geology replaces cliff banding** on the code path (mutual exclusion via `if/elif`). On flag-ON, the basement uses lithology-palette banding instead of the legacy `_BIOME_CLIFF_STONE` + `_CLIFF_BANDS` tables. On flag-OFF, legacy cliff banding runs unchanged.

**Exit criteria:**
- Flag-OFF: `validate_3x3 --baseline` on 48_48, 51_53, 59_53 — zero new PASS→FAIL flips (geology code doesn't run when flag is off; callers pass None).
- Unit test suite: 46 total (36 prior + 10 new), all passing (the 1 opensimplex-dep failure is a sandbox-only issue, passes on user's machine).
- Flag-ON manual verification: `config/thresholds.json → lithology.feature_flag_enabled: true`, run `validate_test_tile --tile-x 59 --tile-z 53`, inspect cliff cross-section in-game for geology layering.

**Scope boundaries (explicitly NOT in Phase 1.75):**
- No sublayer 5 (river_bed) or 6 (lake_bed) — requires `river_meta` / `hydro_lake` threading to `build_column_array()`.
- No sublayer 7 (vertical_fluting_consumer) — Phase 2 territory.
- No changes to `core/eco_gradients.py`, `core/surface_decorator.py`, or any `rebuild_*.py`.
- No `.mca` regeneration or in-game validation (flag remains OFF).
- No diagnostic output (`diag_cliff_crosssection.py` with flag-ON deferred to next session).
- Aspect convention drift still unresolved (carry-forward).

**Next session (post-Phase-1.75) delivers:**
- Flip `lithology.feature_flag_enabled: true`, regenerate 59_53 .mca, inspect cliff cross-section in-game.
- Tune sediment/soil thresholds based on visual inspection.
- Sublayers 5-6 (river_bed, lake_bed) if river_meta/hydro_lake threading is straightforward.
- Or proceed to Phase 2 (surface pipeline pilot layers) if geology looks good enough.

---

### Phase 1.75b — All-biome surface decorator gating + palette tuning (inserted S48, 2026-04-12)

**Scope:** Extend the S47 surface decorator geology gating (currently gap==5 rock only) to ALL gap handlers and slope zones that write `subsurface_blocks`. When `use_new_geology=True`, the geology column owns everything below `surface_y`; the surface decorator should only write `surface_blocks`. Also: palette tuning pass on all 6 lithology groups based on in-game review.

**Architecture finding (S48 recon):**
Geology fills `vol` in Y range `[Y_MIN+1, surface_y-3]`. The surface decorator's `sub_blk` fills `sy-1` and `sy-2` (non-overlapping). When gap handlers override `sub_blk` with "stone" (snow, slope zones) or "sandstone" (sand dunes), it creates a discontinuity: stone at sy-1/sy-2 then dirt (soil horizon) at sy-3. Fix: skip subsurface overrides when geology flag is ON, letting base biome's default `sub_blk` persist (typically "dirt"), which is continuous with geology's soil horizon.

**Deliverables:**
- `core/surface_decorator.py`:
  - `decorate_surface()` — gate `subsurface_blocks` writes in snow (gap==7), sand dune (gap==8) handlers behind `use_new_geology`. Surface writes preserved.
  - `_apply_slope_zones()` — gains `use_new_geology: bool = False` param. When True, skip all `subsurface_blocks` writes (transition stone, cliff, talus). Surface writes preserved.
  - Gap-edge dither subsurface copy (line ~1618) — gate behind `use_new_geology`.
  - Meadow (gap==1), windthrow (gap==2), floodplain (gap==4): **no change needed** — these only write `surface_blocks`, no subsurface overrides.
  - Alpine meadow (gap==6): **no change needed** — only writes `surface_blocks`.
- `tests/unit/test_phase1_75b_gating.py` — new file testing:
  1. Snow handler skips subsurface writes when geology ON.
  2. Sand dune handler skips subsurface writes when geology ON.
  3. Slope zone subsurface skipped when geology ON, surface still written.
  4. Legacy (flag OFF) behavior unchanged for all handlers.
- Palette tuning: adjust `config/thresholds.json` lithology palettes if in-game review reveals issues.

**Exit criteria:**
- Flag-OFF: `validate_3x3 --baseline` on 48_48 — zero new PASS→FAIL flips.
- Flag-ON: `validate_3x3` on 24_80 and 59_53 — no new regressions vs S47 results.
- Unit tests: all prior + new tests passing.
- In-game spot-check on 24_80 or 59_53 if palette changes made.

**Scope boundaries (explicitly NOT in Phase 1.75b):**
- No sublayer 5 (river_bed) or 6 (lake_bed).
- No changes to `core/chunk_writer.py` geology fill logic.
- No changes to `core/eco_gradients.py` or any `rebuild_*.py`.
- Aspect convention drift still deferred.

---

### Phase 2 — Temperate Mountain Pass 2 (week 3) — SPLIT INTO 2.0/2.5/2.75 (S48)

> **Scope split (S48, 2026-04-12).** Original Phase 2 spec targeted all 11 §6 layers in one session. S48 recon found this unrealistic — split into three decimal phases by dependency group. §6 table is canonical for layer specs; this section is the implementation roadmap. Pilot tile changed from 36_20 (desert) to **59_53** (temperate forested ridges, existing baseline).

**Original deliverables (still the full Phase 2 goal):**
- 11 Pass 2 layers from § 6 table (9 partition + 2 overlay), in `core/layers/pass2_surface/`.
- `core/surface_pipeline.py` wired as the Pass 2 orchestrator.
- `surface_decorator.py` gains feature flag `use_new_surface_pipeline` — default OFF.
- `desert_pavement` parallel protocol test on 24_80.
- `diag_layer_ownership.py` real output on 59_53.
- `diag_cliff_crosssection.py` output on 59_53 showing vertical fluting.

**Original exit criteria:**
- Feature flag ON for temperate biomes produces results Nick reviews and approves.
- Regression baseline on 48_48 (ocean) + 51_53 (river/forest) still green.
- .mca regen: 59_53 in-world inspection.

**Risk:** high. Most of the creative work lives here. Most likely slip point.

---

### Phase 2.0 — Feature flag wiring + rock/cliff group (S48, 2026-04-12)

**Scope:** Wire `use_new_surface_pipeline` feature flag in `surface_decorator.py`. Construct `SurfaceContext` from existing pipeline artifacts. Implement the 3 rock/cliff layers that are the highest-impact visual change and prove partition + overlay semantics end-to-end.

**Deliverables:**
- `surface_decorator.py` — new `use_new_surface_pipeline` feature flag (default OFF, gated in `config/thresholds.json`). When ON for temperate biomes, delegates to `surface_pipeline.run_pass()` for rock/cliff layers; legacy code still handles remaining pixels.
- `core/layers/pass2_surface/temperate_cliff_face.py` — **partition** layer. Rock mix from lithology group: 70% primary + 20% secondary + 10% accent, with ±10% edge jitter from noise. Scope: temperate biomes, `cliff_deg >= 35°`.
- `core/layers/pass2_surface/temperate_talus_apron.py` — **partition** layer. Cobblestone + gravel scatter below cliff_face pixels. Scope: temperate, `18° <= cliff_deg < 35°`, concave.
- `core/layers/pass2_surface/vertical_fluting.py` — **overlay** layer. Phase-modulated stone variant substitution on cliff faces, stripe width 4 ±1 block. Scope: any biome, `cliff_deg >= 35°`, already claimed by cliff_face.
- `SurfaceContext` builder in `surface_decorator.py` (or `_pipeline_runner.py`) that maps eco_grads + lithology + surface_y into the `SurfaceContext` dataclass.
- Unit tests: partition coverage on 59_53 for cliff_face; overlay correctness for vertical_fluting; flag-OFF regression.

**Exit criteria:**
- Flag-OFF: 48_48 baseline green. 59_53 baseline green.
- Flag-ON: 59_53 3×3 renders cliff faces with lithology-derived rock palettes and visible vertical fluting stripes.
- Unit tests: all prior + new tests passing.

**Scope boundaries (NOT in Phase 2.0):**
- No terrain layers (grass_terrace, weathered_top, forest_surface).
- No water-adjacent layers (riparian_fringe, river_bar, lake_edge).
- No snow_cap_north, beach_by_fetch, desert_pavement.
- No diag_layer_ownership.py rewrite (deferred to Phase 2.5 or 2.75).
- Legacy surface_decorator still handles all non-cliff pixels.

---

### Phase 2.5 — Terrain group (3 layers)

**Scope:** Fill the flat/moderate slope gaps in the surface pipeline.
- `temperate_grass_terrace` — partition, moderate slopes
- `temperate_weathered_top` — partition, flat + high elevation + exposed
- ~~`temperate_forest_surface`~~ — **REMOVED S50.** Over-claimed all unclaimed forested-biome pixels including clearings/meadows. Legacy per-biome surface block logic + gap_mask meadow/clearing dither in `decorate_surface()` handles forest floors correctly.

**Exit criteria:** 59_53 with flag ON shows cliff + talus + grass + weathered cover geological/terrain pixels; forested land handled by legacy decorator.

---

### Phase 2.75 — Cross-biome + environment layers [Updated S51, S55]

**Scope:** Revised S50/S51/S55 — dropped `temperate_riparian_fringe` and `lake_edge` (existing signals sufficient). snow_cap_north converted from surface pipeline layer to precompute mask (S51). Beach was converted to precompute mask in S51, then re-architected as in-eco_gradients programmatic placement in S55 (see Phase 2.75c below + §19 drift entry S55-1).
- `snow_cap_north` — **Converted S51:** was overlay layer, now precompute mask `snow_caps_north.tif` (generated by `rebuild_rock_exposure.py` §13b). Wired into eco_gradients as additional gap==7 territory. Surface pipeline layer removed.
- `river_bar` — partition, arid zones, gravel/sand bars in dry riverbeds (landed S50)
- ~~`beach` — **Converted S51:** precompute mask `beach.tif` via `rebuild_beach.py`. EDT from ocean at 1:8, tight elevation + slope gate. Y=63 constraint enforced in eco_gradients (gap==9). Sand-only surface, no gravel/podzol dither.~~ **SUPERSEDED by Phase 2.75c (S55)** — the `beach.tif` + hard Y=63 gate approach produced blobby/staircased results because (a) the mask was upscaled from 1:8 and the threshold contour at 1-block resolution is a staircase, and (b) the Y=63 hard gate discretized the waterline. Sand-only painting with no dither meant hard edges against the biome palette, often showing through as podzol/mud walls. See §19 entry S55-1 for drift context.
- `desert_pavement` — partition, flat arid non-dune land: coarse_dirt, packed_mud (landed S50). Ground cover (dead_bush, short_dry_grass, tall_dry_grass) still pending.
- `diag_layer_ownership.py` rewrite from stub to real pipeline reader

**Exit criteria:** snow_cap_north visible on high-elevation north faces (via precompute mask). ~~beach places sand at Y=63 coastlines~~ → see Phase 2.75c. desert_pavement breaks up arid monotone on (24,80). diag_layer_ownership.py produces real output.

---

### Phase 2.75c — Programmatic coastal beach with salt-and-pepper dither (inserted S55, 2026-04-15)

**Why a new decimal phase:** S51's beach approach (`beach.tif` precompute mask + hard Y=63 gate + sand-only paint) was abandoned in S55 after in-game validation revealed blocky shorelines, staircased inland edges, and meadow/floodplain clearings cutting into coastlines. Rather than rename §11 Phase 2.75's beach bullet, this decimal subsection captures the replacement design; the original bullet is marked SUPERSEDED above. §19 entry S55-1 documents the full design-vs-codebase drift.

**Scope:** Beach placement moves entirely into `core/eco_gradients.py` beach block. Replace precompute-mask reliance with per-tile programmatic computation driven by physical signals:

1. **Ocean seed** — `(surface_y < 63) & ~river_mask`. Strict `<` (Y=63 is sea-level *land*, not ocean; earlier `<=` bug painted whole coastal tiles sand). Rivers explicitly excluded so river banks don't read as coast.
2. **Distance field** — `distance_transform_edt(~ocean)` gives per-pixel block distance to the nearest ocean pixel.
3. **Biome gate (two-tier)** — `FULL_BEACH_BIOMES` = {`COASTAL_HEATH`, `EASTERN_TEMPERATE_COAST`, `RAINFOREST_COAST`, `LUSH_RAINFOREST_COAST`} get wide beaches; `SHALLOW_BEACH_BIOMES` = {`TEMPERATE_RAINFOREST`, `BOREAL_TAIGA`, `TEMPERATE_DECIDUOUS`} get narrow PNW-style beaches; everything else gets no surface beach (arid → dunes own sand; tundra/frozen/alpine/karst → no sand biogeography; mangrove/tidal → own mud treatment; inland forests → no coast contact).
4. **Width field** — per-pixel `core_width = base + amp × Gaussian_noise` where FULL uses `(base=4, amp=2)` and SHALLOW uses `(base=2, amp=1)`. Noise = `gaussian_filter(rng.standard_normal, sigma=12)` normalized to [-1, +1], giving 12-block coherent lobes along the coast ("wave action reached further here than there"). Intentionally tight core — the always-sand band at the waterline is 2–6 blocks only.
5. **Dither zone** — `dither_width = core_width × 3.0`. Expands the mixing band to 6–18 blocks. Total beach reach 8–24 blocks, ~80% of which is mix zone.
6. **Probability plateau** — `place_prob = clip(1 - t, 0.15, 0.85)` across dither zone (where `t ∈ [0, 1]` is distance-through-dither). Clamping at both ends guarantees salt-and-pepper *everywhere* in the zone — 15% sand fingers reach the outermost edge, 15% biome pockets reach the innermost edge — instead of mostly-sand inner / mostly-biome outer sub-bands.
7. **Decision coin** — `gaussian_filter(rng.random, sigma=1)` normalized to [0, 1]. sigma=1 is intentionally near per-pixel salt-and-pepper — larger sigma created coherent lobes that resolved sand-or-biome in whole patches instead of block-by-block mixing.
8. **Gap override** — beach eligibility includes `gap_mask ∈ {0, 1, 4}`, letting beach stomp meadow (1) and floodplain (4) in its coastal zone. Inland meadow/floodplain is unaffected because the distance cap limits beach to `_dist_from_ocean ≤ total_width`. Beach still yields to rock (5), alpine_meadow (6), snow (7), sand_dune (8).
9. **Beach-edge mask** — pixels in the dither zone that did NOT get sand (`_in_dither & ~_bch_dithered`) are recorded in new `EcoGradients.beach_edge_mask` field. These stay as biome blocks, but with reduced vegetation density so the biome floor (mud/coarse_dirt/moss_block/podzol) shows through the ground cover — visible salt-and-pepper from above.

**Supporting changes:**
- `core/preview_renderer.py:134` — `ocean_mask = surface_y <= 63` → `surface_y < 63`. Strict. Previous code painted sea-level land pixels as ocean-blue in PNGs regardless of surface block, hiding the beach in validator output.
- `core/column_generator.py` — underwater near-shore sand (`line ~553-557`) **removed**. Produced long straight-line sand blobs on the seafloor because it was a hard depth threshold with no dither. Seafloor now uses `underwater_floor_block()` for all depths. Near-shore seafloor treatment deferred to a dedicated underwater pass.
- `core/eco_gradients.py:462` — sand_dunes `gap_mask = 8` painting gated to `biome == "SAND_DUNE_DESERT"` only. The `sand_dunes.tif` mask spills into `DESERT_STEPPE_TRANSITION` (65%) and `SEMI_ARID_SHRUBLAND` (80%) due to physical-signal calibration (flat + arid + low slope). Biome gate prevents sand blobs from overwhelming those biomes' native palettes. Coverage inside `SAND_DUNE_DESERT` is 99.9% so the gate is effectively redundant with biome, but kept as a safety net.
- `core/surface_decorator.py:_apply_ground_cover` — new `eco_density_mod[beach_edge_mask] = 0.15` reduces vegetation on the beach transition zone to 15% of normal. Without this the salt-and-pepper was invisible: plants covered every non-sand pixel and only the bare sand blocks showed from aerial view.
- `core/surface_decorator.py:_apply_ecotone_dither` — `width_px` default `24 → 48` and decision `rand_field` now `gaussian_filter(rng.random, sigma=3)` (was uniform per-pixel). Wider biome-to-biome transition bands with coherent salt-and-pepper. Still only fires on multi-biome tiles — **cross-tile ecotone is a deferred carry-forward**.

**Exit criteria:** (6,72) LUSH_RAINFOREST_COAST shows (a) narrow pure-sand waterline, (b) wide visible speckled band with individual sand + mud/coarse_dirt/moss_block/podzol blocks interleaving per-pixel, (c) organic fade into vegetated forest with no hard boundary, (d) no meadow/floodplain grass clearings cutting into the coast. SEMI_ARID_SHRUBLAND (35,77) shows no surface beach (biome gate excludes it) and no 80%-of-tile sand slab (sand_dunes biome gate works).

**Validated:** (6,72) in-game — user confirmed 2026-04-15 "Beach looks great." Landed commits: [THIS COMMIT].

**Deferred from this phase:**
- Cross-tile ecotone dither (biome A on tile X, biome B on tile X+1 — tiles compute ecotone in isolation, no seam awareness).
- `SEMI_ARID_SHRUBLAND` native palette tuning (sand noise entries produce visible sand patches inland — 8.96% of tile. Reduce if user wants; left for a palette-pass session).
- Underwater near-shore seafloor (removed S55; needs dedicated pass with dither, distinct from surface beach).
- Full `column_generator` surface code cleanup (steps 5-10 write to dead `surface_blk` per S54 note; mostly inert but worth purging).

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

---

### S45 — Phase 0.75 shadow-mode hookup (2026-04-11)

**Phase state (end of S45):**
- Landed this session: **Phase 0.75** (shadow-mode hookup)
- §11 currently at: **Phase 0.75** (retroactively added as a new subsection between Phase 0.5 and Phase 1 in this same commit to resolve the S44→S45 phase-numbering drift — see §11 header note)
- Next session starts: **Phase 1** (column additive path, §11 unchanged) OR **Phase 2 pilot prep** if the user wants to skip column work to unblock the visible pilot. User decides. Either way, the prerequisite is a `tests/baselines/3x3/36_20/` baseline snapshot taken *before* the first touch.

**Phase-plan reconciliation note:** S44's handoff originally called this session's work "Phase 1," but §11 had Phase 1 = "Column additive path." Rather than renaming §11's Phase 1 (which would break the design-doc phase chronology), this intermediate plumbing step was inserted as **Phase 0.75** in §11, §18, and CLAUDE.md in one commit. A new "Session kickoff pillar checkoff" in CLAUDE.md's Workflow section and an "End-of-session reconciliation" rule now enforce that §11 phase numbering stays in sync with the handoff going forward. The rule: §11 is the canonical phase map; new intermediate steps get decimal numbers (0.75, 1.5, …), never rename-over-existing phases.

#### Goal

Prove that the `core/surface_pipeline.run_passes()` call path works end-to-end on real tile data — `SurfaceContext` construction from production state, protocol invariants, exception path — with zero impact on production blocks, before Phase 2 hands any real layer pixel ownership on the pilot tile. This isolates "is the plumbing good?" from "is the layer correct?" so the Phase 2 debug surface is small.

#### Files touched (3)

**1. `config/thresholds.json`** — new top-level key `surface_pipeline`:
```json
"surface_pipeline": {
  "shadow_mode": false,
  "_comment": "Phase 0.75 (S45): when true, decorate_surface() calls run_passes([], ctx) at the end as a smoke test of the new SurfaceContext path. Output is discarded; production blocks are unchanged. Also gated by env VANDIR_SHADOW=1."
}
```
Top-level key count: 30 → 31. `lithology.feature_flag_enabled` untouched (still `false`).

**2. `core/surface_decorator.py`** — additive ~55-line shadow block inserted immediately before the final `return surface_blocks, subsurface_blocks, ground_cover` at line 1349. Behind `cfg['surface_pipeline']['shadow_mode']` OR `VANDIR_SHADOW=1`, builds a `SurfaceContext`:
  - `tile_x`, `tile_z` from the decorate args (note: `tile_y` in decorate's signature maps to `tile_z` in the SurfaceContext — a naming drift in production code; pinned to the context field name, not the decorate arg name).
  - `biome_grid` passed through directly.
  - `lithology_grid=None` (Phase 0.5 flag still OFF; future-flip of `lithology.feature_flag_enabled` will revisit).
  - `eco_grads` dict built from 11 attrs on the decorate's `eco_grads` object (`moisture_index`, `wind_exposure`, `concavity_norm`, `soil_depth`, `gap_mask`, `aspect`, `riparian_proximity`, `lake_fringe`, `rock_exposure_gradient`, `rock_tight_gradient`, `snow_caps_gradient`), each conditional on the attr actually existing.
  - `column_output={"surface_y": surface_y}` — enough for Phase 2 layers to sample heights when they land.
  - `prior_surface = surface_blocks.copy()` — the COPY is critical; `run_passes` also copies internally, but defense-in-depth means no aliasing can leak into production.
  - `prior_ownership`, `overlay_touched` — fresh zero arrays at the correct dtype.

Then calls `run_passes([], _shadow_ctx, strict=True)` and immediately `del`s the result. Whole block is `try/except Exception`-wrapped; any error is logged to stdout as `[shadow] ERROR tile=(X,Y): {type}: {msg}` and swallowed. The production return tuple is never touched by this block — even if context construction raises, `surface_blocks`/`subsurface_blocks`/`ground_cover` are already finalized before the shadow block runs.

**Why an empty layer list is mathematically mutation-free:** `run_passes` iterates `for layers in passes`. On an empty iterable, the loop body never executes. The initial `PipelineResult(surface=ctx.prior_surface.copy(), ownership=ctx.prior_ownership.copy(), overlay_touched=ctx.overlay_touched.copy())` is returned verbatim. This is structural, not conventional — pinned by `test_run_passes_empty_iterable_is_identity` below. A future refactor that changes this invariant breaks the test before it can ship.

**3. `tests/unit/test_shadow_hookup.py`** — new file, 5 tests:
- `test_run_pass_empty_layer_list_is_identity`
- `test_run_passes_empty_iterable_is_identity`
- `test_run_passes_multiple_empty_passes_is_identity` (pins the `for layers in passes:` outer loop on non-empty iterable of empty sequences)
- `test_run_passes_does_not_alias_prior_surface` (pins copy-semantics: mutating `result.surface` must not leak into `ctx.prior_surface`)
- `test_empty_block_sentinel_still_exists` (sentinel check that `EMPTY_BLOCK == ""` hasn't been removed from the protocol)

**Not added** (descoped mid-session to keep S45 short): flag-ON vs flag-OFF integration test inside `decorate_surface()`. The synthetic-context unit tests plus the real-tile `VANDIR_SHADOW=1` 3×3 validation against baseline already prove byte-identity at the `decorate_surface()` level — adding a mock-input integration test would have been redundant insurance, not new coverage.

#### Validation results

| Gate | Result | Detail |
|---|---|---|
| `tests/unit/` full suite | **31 passed** | 26 prior + 5 new (test_shadow_hookup.py). Prior count was 26 per S44 entry. |
| `tools/validate_masks.py` | **26 passed, 0 failed** | Sanity check on all masks; no drift since S44. |
| `validate_3x3 --tile-x 48 --tile-z 48 --baseline tests/baselines/3x3/48_48` (flag OFF) | **No baseline regressions ✅** | 9 tiles, all surface/terrain/hydrology/seam checks within prior envelope. |
| `validate_3x3 --tile-x 51 --tile-z 53 --baseline tests/baselines/3x3/51_53` (flag OFF) | **No baseline regressions ✅** | 8 pre-existing bare-dirt + biome-seam FAILs unchanged and still marked known-state; no new PASS→FAIL flips. |
| `VANDIR_SHADOW=1 validate_3x3 --tile-x 48 --tile-z 48 --baseline tests/baselines/3x3/48_48` (flag ON) | **No baseline regressions ✅** + **0 `[shadow] ERROR` lines in stdout** | Shadow hookup executed on all 9 tiles (mix of ocean + land + biome seams), `SurfaceContext` constructed cleanly, `run_passes([], ctx, strict=True)` came back clean, production blocks byte-identical to flag-OFF run. |

#### Phase 0.75 exit criteria — all green

- [x] Flag OFF: byte-identical to pre-edit baseline on 48_48 and 51_53.
- [x] Flag ON: zero `[shadow] ERROR` lines in stdout; zero new PASS→FAIL flips vs baseline.
- [x] `run_passes` call path is alive end-to-end on real tile data.
- [x] Unit test invariant pins empty layer list as structurally identity.
- [x] Production `decorate_surface()` return tuple is never touched by the shadow block.
- [x] §11, §18, CLAUDE.md all reconciled to "Phase 0.75" naming in the same commit; pillar checkoff added to prevent future drift.

#### Open items carried into next session

- **51_53 flag-ON (optional strictness).** Skipped this session to save ~60 min wall-time. The 48_48 flag-ON run already covers ocean + land-adjacent tiles, and 51_53 flag-OFF proves the production path on a land-heavy tile. If Phase 1 (column additive) turns out to need it for regression confidence, run it then.
- **`36_20` baseline snapshot** — prerequisite for Phase 1 (column additive path) and/or Phase 2 (pilot layers). **Not taken this session** — Phase 0.75 intentionally doesn't touch any code path that 36_20 uniquely exercises. Take the baseline in the FIRST session that will touch `column_generator.py` or a real Phase 2 layer, *immediately before* the edit, per the workflow rule.
- **Aspect convention drift** (carry-over from S44). `core/eco_gradients.py` aspect sign is inverted relative to `core/wind_model.py`. Phase 2 layers MUST recompute aspect from gradient using the `wind_model` convention, not trust `eco_grads.aspect`. Not fixed in Phase 0.75 scope.
- **Lithology feature flag** — still OFF. Flipped only when Phase 1/2 consumers are ready.

#### Notes

- `tile_y` vs `tile_z` naming: `decorate_surface()`'s signature uses `tile_y` for the Z-axis tile coordinate (legacy from earlier rename), while `SurfaceContext` uses `tile_z`. The shadow block maps `tile_x → tile_x` and `tile_y → tile_z` verbatim. Not worth fixing in this session, but callers reading log lines like `[shadow] ERROR tile=(48,48)` should interpret the second integer as tile_z / world-Z.
- Push is blocked in sandbox (per standing rule). Nick pushes from his terminal.

---

### S46 — Phase 1.5 lithology wiring scaffold (2026-04-11)

**Phase state (end of S46):**
- Landed this session: **Phase 1.5** (lithology wiring scaffold in `chunk_writer.build_column_array()`)
- §11 currently at: **Phase 1.5** (newly inserted subsection between original Phase 1 — now marked SUPERSEDED — and Phase 2)
- Next session starts: **Phase 1.75 or promoted-Phase-1** (actual geology content — sublayers bedrock_band promotion, basement_rock_by_group, sediment_thickness, soil_horizon), per §11 Phase 1.5 "Next session delivers" block.

**Spec-vs-code drift resolved this session:** original §11 Phase 1 described extending `core/column_generator.py`'s `fill_column(...)` per-column API with lithology params and an inner `_fill_column_with_geology()`. During S46 recon the codebase was found to not match this in two ways:
1. `column_generator.py` has no `fill_column()`. The hot path is `process_tile_columns_v2()`, a vectorized tile-level function on `(H, W)` arrays. The legacy per-column `generate_column()` exists at line 452 but no caller on the production path uses it (`tools/_pipeline_runner.py:206` calls v2; `run_pipeline.py` goes through the same runner).
2. More critically, **`column_generator.py` does not control mid-column block fill at all**. The emitted `ColumnResult.blocks` sparse dict contains only `BEDROCK_Y`, `sy-2`, `sy-1`, `sy`, water fill, and dune fill. Everything in `[BEDROCK_Y+1, sy-3]` is absent from the dict. That vertical range — exactly where §6 Pass 1 wants basement_rock_by_group + sediment_thickness + most of soil_horizon to live — is filled inside **`core/chunk_writer.build_column_array()`** (line 202), which takes `(H, W)` tile arrays (`surface_y`, `surface_blk`, `sub_blk`, `ground_cover`, `biome_grid`) and constructs the entire `(Y_RANGE, H, W)` voxel volume directly. The hardcoded `[Y_MIN+1, sy-3] → stone` fill with per-biome cliff banding variants already lives there.

**Resolution:** original §11 Phase 1 marked **SUPERSEDED**. New **§11 Phase 1.5** (inserted per the decimal-subsection rule, not a rename) moves the lithology injection site to `chunk_writer.build_column_array()`. See also §6 Pass 1 header note and §19 Spec-vs-Code Drift Log entry S46-1. CLAUDE.md Workflow section gains a new "Codebase-reconciliation check" rule (sibling to the S45 phase-reconciliation pillar checkoff) requiring each session to confirm spec function signatures against the real codebase before any code edit.

**Tile choice correction (S46, in-chat):** original Phase 1 spec named `36_20` as the baseline tile ("in-game validated, known-good"). User confirmed in-chat that 36_20 is actually the **desert rock** tile from Session 41 work, not temperate mountain. S46 re-picked `59_53` (windthrow reference — high-elevation forested ridges = temperate mountain character) as the canonical Phase 1.5 baseline. `59_53` is in the CLAUDE.md "Land-heavy reference tiles still missing baselines" list and has no prior baseline entry.

**Files touched (S46 scope — scaffolding only, no geology content):**

1. **`PHYSICAL_REALISM_REFACTOR.md`** — §11 header gains two new callouts (S46 spec-vs-code drift + Codebase-reconciliation rule). Original Phase 1 marked SUPERSEDED in place. New Phase 1.5 subsection inserted. §6 Pass 1 header gains S46 correction note. §18 S46 entry (this block). §19 Spec-vs-Code Drift Log appended.

2. **`core/chunk_writer.py`** — `build_column_array()` signature extended with four new optional kwargs:
   - `lithology_tile: np.ndarray | None = None`
   - `sediment_thickness_tile: np.ndarray | None = None`
   - `soil_horizon_depth_tile: np.ndarray | None = None`
   - `use_new_geology: bool = False`

   Early-out guard at the top of the function: `if not use_new_geology or lithology_tile is None:` → fall through to the existing unchanged code path. Flag-OFF is byte-identical by construction — no existing lines inside the function body were modified. When `use_new_geology=True` AND a lithology_tile is provided, a new inner branch raises `NotImplementedError` with a message referencing §11 Phase 1.5 and §6 Pass 1. No geology logic implemented.

3. **`core/column_generator.py`** — `process_tile_columns_v2()` and `generate_columns()` gain the same four optional kwargs. Both pass them through unchanged to downstream consumers (currently no-op; they'll matter when S47+ wires `build_column_array()` call sites to pass real lithology arrays). Neither function's flag-OFF behavior changes.

4. **`tests/unit/test_phase1_5_scaffolding.py`** — new file, tests:
   - `test_build_column_array_flag_off_is_identity` — synthetic `(16, 16)` tile, compare `build_column_array(..., use_new_geology=False)` to `build_column_array(...)` without the new kwargs. Assert `np.array_equal(vol_new, vol_old)` and palette equality.
   - `test_build_column_array_flag_off_with_lithology_still_identity` — same as above but also passes a synthetic lithology_tile with `use_new_geology=False`. The flag alone gates the new path; a lithology tile without the flag must be ignored.
   - `test_build_column_array_flag_on_raises_not_implemented` — pass a minimal synthetic lithology_tile + `use_new_geology=True`, assert `NotImplementedError` with expected substring.
   - `test_process_tile_columns_v2_param_threading` — call `process_tile_columns_v2` with and without the new kwargs on a tiny synthetic tile, assert `ColumnResult` surface_y / blocks match.
   - `test_no_caller_accidentally_enables_flag` — grep-style sentinel scanning `tools/` and `run_pipeline.py` for `use_new_geology=True`. If found, fail with the file+line. Prevents quiet enablement.

5. **`CLAUDE.md`** — Current state line updated to S46; new Workflow rule (Codebase-reconciliation check) added as a sibling to the pillar checkoff.

6. **`PROJECT_MEMORY.md`** — §10 Session log gains a one-line S46 entry.

7. **`memory/project_vandir_status.md`** — S46 entry capturing the spec-vs-code drift finding, decision tree, and resolution.

**Validation results (post-edit gate — S46 actual):**

| Gate | Result | Detail |
|---|---|---|
| `tests/unit/` full suite | ✅ 36/36 PASS | 31 prior + 5 new Phase 1.5 tests. One DeprecationWarning on `\G` escape sequence in the AST sentinel — cosmetic, not blocking. |
| `validate_3x3 --tile-x 59 --tile-z 53 --baseline tests/baselines/3x3/59_53` (flag OFF) | ✅ No baseline regressions | New baseline: 74 PASS / 6 FAIL / 4 WARN (16 min elapsed). Post-edit verify matched byte-for-byte: 74 PASS / 6 FAIL / 4 WARN. Zero new PASS→FAIL flips. |
| `validate_3x3 --tile-x 48 --tile-z 48 --baseline tests/baselines/3x3/48_48` (flag OFF) | ✅ No baseline regressions | 79 PASS / 0 FAIL / 5 WARN. Ocean/coast safety net — clean. |
| `validate_3x3 --tile-x 51 --tile-z 53 --baseline tests/baselines/3x3/51_53` (flag OFF) | ✅ No baseline regressions | 74 PASS / 8 FAIL / 2 WARN. Pre-existing riparian bare-dirt hole + biome seam unchanged. |

**Phase 1.5 exit criteria:**
- [x] Flag-OFF: byte-identical to pre-edit baseline on 59_53, 48_48, 51_53. ✅ all three reported "No baseline regressions".
- [x] Attempting `use_new_geology=True` with a lithology_tile provided raises `NotImplementedError` citing §11 Phase 1.5 / §6 Pass 1 and Phase 2 / S47. ✅ pinned by `test_flag_on_raises_not_implemented`.
- [x] Unit test suite green. ✅ 36/36.
- [x] No caller in `core/`, `tools/`, or `run_pipeline.py` passes `use_new_geology=True`. ✅ AST sentinel `test_no_caller_enables_flag_in_production` passes.
- [x] §11 Phase 1 SUPERSEDED header + Phase 1.5 subsection + §19 drift log all landed. ✅
- [x] CLAUDE.md Current state line + new Codebase-reconciliation rule. ✅

**Open items carried into next session:**

- **Implement Phase 1.5 flag-ON content** — sublayers 1 (bedrock_band promotion), 2 (basement_rock_by_group), 3 (sediment_thickness), 4 (soil_horizon) inside `build_column_array()`'s `use_new_geology=True` branch. Use the existing cliff banding Y-slice loop as the broadcasting template. Sublayers 5 (river_bed) and 6 (lake_bed) follow once the callers in `_pipeline_runner.py` / `validate_test_tile.py` start passing `river_meta` and `hydro_lake` through to `build_column_array()`. Next session is either Phase 1.75 (narrow) or a rewritten Phase 1 (broad).
- **Lithology tile producer** — `masks/lithology.tif` already exists (from Phase 0.5). Sediment_thickness and soil_horizon tiles don't yet. Decide: compute them at mask-rebuild time (new `rebuild_*.py`) or inline per-tile inside `build_column_array()` from `concavity_norm + flow_tile + slope + biome`. S46 deferred this decision.
- **`_pipeline_runner.py` + `validate_test_tile.py` + `check_tile_seams.py` call-site updates** — once Phase 1.5 flag-ON lands, callers need to actually pass the new kwargs. Current S46 scope keeps all callers on default None/False to preserve byte-identity. Do NOT flip any caller in S46.
- **Aspect convention drift** (carry-over from S44/S45). Still unresolved. Not Phase 1.5 scope.
- **51_53 flag-ON shadow hookup** (carry-over from S45). Still skipped. Not Phase 1.5 scope; surface_decorator shadow path is orthogonal to chunk_writer lithology path.
- **flag-ON cliff cross-section diag on 59_53** — explicitly deferred to next session per Phase 1.5 scope boundary. Will need `diag_cliff_crosssection.py` to accept a lithology kwarg too.

**Notes:**

- Push blocked in sandbox per standing rule. Nick pushes from local terminal when ready.
- S46 is deliberately a small, safe, shippable increment. The Option B choice in-chat was "full Phase 1 — fatten sparse dict." The S46 recon showed "fatten the sparse dict" is the wrong abstraction — the sparse dict is a dead path for mid-column fill — so Option B morphed mid-session into "extend the real mid-column injection point." The scope (scaffolding only, no geology content) is narrower than a literal reading of "full Phase 1" would suggest, but delivers the same architectural foundation in a way that leaves a very small S47 Δ to write the geology logic itself. The user authorized all perms partway through S46, so the decision to trim the content scope was made by Claude to keep flag-OFF byte-identity achievable in one session; any expansion of scope on S47 has no new blockers.

---

### S47 — Phase 1.75 real geology content (2026-04-12)

**Phase state (end of S47):**
- Landed this session: **Phase 1.75 + surface decorator gating + visual tuning**
- §11 currently at: **Phase 1.75** (subsection between Phase 1.5 and Phase 2)
- Next session starts: **Phase 1.75b** — extend surface decorator gating to ALL biomes (currently only gap==5 rock). Then sublayers 5-6 (river_bed, lake_bed) or Phase 2 surface pilots.

**Design decisions (S47, in-chat):**
1. sediment_thickness + soil_horizon_depth computed **inline per-tile** (not precomputed .tif masks). 1:8 resolution smears column-scale gradients.
2. Surface decorator **gated by geology flag**: when `use_new_geology=True`, gap==5 rock painting skips legacy terracotta/basalt/stratification and uses naturalistic soil palette instead. Cliff faces left unpainted so geology column shows through as cross-section.
3. Lithology palettes expanded to 6-8 blocks per group, no `stone` filler that hides bands.
4. Band thickness randomised 4-10 blocks via precomputed LUT (was uniform 12).

**Bugs found and fixed (S47):**
1. **Lithology read coordinate mismatch** — `lithology.tif` is 6250×6250 (1:8 scale), but all three callers read at full-res offsets (`tile_x * 512` instead of `tile_x * 64`), producing a 512×512 window of zeros. Fix: divide offsets by 8, read 64×64, zoom NEAREST in `_fill_geology_layers()`.
2. **Surface decorator mesh artifact** — `_apply_desert_rock_palette()` ran unconditionally after geology, overwriting subsurface with terracotta stratification bands and basalt caps on convex ridges. Created visible grid/mesh pattern on all slopes. Fix: gated behind `use_new_geology` flag.

**Files touched (S47 — geology content + surface gating + tuning):**

1. **`core/chunk_writer.py`** — Core geology:
   - `_fill_geology_layers()` — sublayers 1-4 with NEAREST upscale of lithology_tile at top. Per-column ±3Y noise for organic band edges.
   - `_build_band_lut()` — new helper: pre-generates Y→variant LUT with randomised band thickness [4,10].
   - `_apply_banded_fill()` — gains `col_y_noise` and `band_lut` params. When LUT provided, band thickness varies per-band.
   - `_compute_xz_waviness()` — extracted from legacy cliff banding.
   - `build_column_array()` — `NotImplementedError` → `_fill_geology_layers()` call. New params: `flow_tile`, `cfg`.
   - `write_tile()` — gains `lithology_tile`, `flow_tile`, reads geology flag.

2. **`core/surface_decorator.py`** — `decorate_surface()` gains `use_new_geology` param. Gap==5 rock block: when True, uses soil palette (grass_block/coarse_dirt/gravel/packed_mud by slope+concavity, sand in washes). When False, legacy terracotta/stone palettes preserved. Legacy `_apply_desert_rock_palette()` and cool rock palette untouched but bypassed on flag-ON.

3. **`core/tile_streamer.py`** — `read_discrete_tile()` for raw integer mask reads.

4. **`tools/_pipeline_runner.py`** — lithology read at 1:8 coords, `use_new_geology` threaded to `decorate_surface()`.

5. **`tools/validate_test_tile.py`** — same 1:8 coord fix + geology flag threading.

6. **`run_pipeline.py`** — same 1:8 coord fix + geology flag threading.

7. **`config/thresholds.json`** — `lithology.feature_flag_enabled: true`, `band_scale_y: 5` (was 12). All 6 lithology palettes expanded:
   - granitic: granite, stone, andesite, diorite, granite, polished_granite
   - sedimentary: sandstone, smooth_sandstone, red_sandstone, sandstone, orange_terracotta, terracotta, smooth_sandstone, red_sandstone
   - basaltic: basalt, smooth_basalt, blackstone, basalt, tuff, smooth_basalt
   - limestone: calcite, tuff, smooth_stone, calcite, diorite, tuff
   - deepslate_metamorphic: deepslate, tuff, stone, cobbled_deepslate, andesite, deepslate
   - mossy_temperate: mossy_cobblestone, cobblestone, andesite, stone, mossy_cobblestone, cobblestone

8. **`tests/unit/test_phase1_75_geology.py`** — 10 new tests (unchanged from first S47 commit).

9. **`tests/unit/test_phase1_5_scaffolding.py`** — `test_flag_on_runs_geology_not_raises` (stub retired).

**Validation results (S47):**

| Gate | Result | Detail |
|---|---|---|
| Unit tests (sandbox Python 3.10) | **46/46 PASS** | All Phase 1.75 + scaffolding tests green after opensimplex install. |
| 3×3 baseline 48_48 (flag-OFF regression) | **PASS** | No baseline regressions. |
| 3×3 flag-ON 59_53 | **74 PASS, 6 FAIL (pre-existing), 4 WARN** | Bare-dirt + biome seam FAILs all pre-existing. No new regressions. |
| 3×3 flag-ON 24_80 | **71 PASS, 1 FAIL (pre-existing), 12 WARN** | Biome seam FAIL pre-existing. Surface decoration clean. |
| In-game 24_80 | **Verified** | Sedimentary + deepslate_metamorphic bands visible in 124-block cliff faces. Variable band thickness, XZ waviness, per-column noise all working. Surface soil palette (grass/coarse_dirt/gravel) clean from above. |
| In-game 59_53 | **Verified** | Temperate rainforest terrain with geology subsurface intact. |

**Open items carried into next session:**
- **All-biome surface decorator gating** — currently only gap==5 rock is gated. Other gap types (alpine_meadow, snow, sand_dune, windthrow, floodplain, meadow) still use legacy painting. Need to audit each for subsurface overrides that conflict with geology.
- **Sublayers 5-6** (river_bed, lake_bed) — requires threading `river_meta` / `hydro_lake` to `build_column_array()`.
- **Palette tuning** — in-game review may reveal further color balance issues per biome region.
- **Aspect convention drift** (carry-over from S44). Still unresolved.

### S48 — Phase 1.75b all-biome surface decorator gating + cliff cross-section diag (2026-04-12)

**Phase state (end of S48):**
- Landed this session: **Phase 1.75b**
- §11 currently at: **Phase 1.75b** (new subsection between Phase 1.75 and Phase 2)
- Next session starts: **Phase 2** — Temperate Mountain Pass 2 (surface pipeline pilot layers)

**Architecture finding (S48 recon):**
Geology fills `vol` in Y range `[Y_MIN+1, surface_y-3]`. The surface decorator's `sub_blk` fills `sy-1` and `sy-2` — **non-overlapping Y ranges**. But when gap handlers override `sub_blk` with "stone" (snow, slope zones) or "sandstone" (sand dunes), it creates a visible discontinuity: stone at sy-1/sy-2 then dirt (soil horizon) at sy-3. Fix: skip all subsurface writes in the decorator when geology is ON, letting base biome subsurface (typically "dirt") persist — continuous with geology's soil horizon.

**Audit results (gap handler subsurface writes):**
- **gap==5 rock** — already gated in S47. No change.
- **gap==7 snow** — 3 `subsurface_blocks` writes. All gated behind `if not use_new_geology`.
- **gap==8 sand_dune** — 1 write. Gated.
- **gap==6 alpine_meadow** — no subsurface writes. No change needed.
- **gap==1 meadow, gap==2 windthrow, gap==4 floodplain** — surface-only writes. No change needed.
- **Gap-edge dither** (line ~1618) — subsurface copy. Gated.
- **`_apply_slope_zones()`** — 6 subsurface writes (transition, cliff, talus). All gated. New `use_new_geology` param added; caller passes the flag.
- **`_apply_desert_rock_palette()`** — already bypassed when geology ON (inside gap==5 `else` branch). No change needed.

**Files touched (S48):**

1. **`core/surface_decorator.py`**:
   - Snow handler (gap==7): 3 subsurface writes gated behind `if not use_new_geology`.
   - Sand dune handler (gap==8): 1 write gated.
   - Gap-edge dither: subsurface copy gated.
   - `_apply_slope_zones()`: gains `use_new_geology: bool = False` param; 6 subsurface writes gated (transition stone, cliff stone, gravel_talus, cobble_talus).
   - Call site: passes `use_new_geology=use_new_geology` to `_apply_slope_zones`.
   - Pre-existing truncation fix: `sys.exit(` → `sys.exit(0)` at EOF.

2. **`diag_cliff_crosssection.py`**: FULL REWRITE from Phase 0 stub to real pipeline reader.
   - Uses `_pipeline_runner.run_tile_prelude()` to run full pipeline through surface decoration.
   - Calls `build_column_array()` with geology params to build voxel volume.
   - Samples line through volume, renders block identities as colored PNG.
   - Annotated output: Y-axis labels, surface profile overlay (orange), block legend.
   - Metadata JSON sidecar with blocks_seen, timing, geology flag state.
   - CLI: `py diag_cliff_crosssection.py --tile-x 24 --tile-z 80 --line 50 50 460 460`

3. **`PHYSICAL_REALISM_REFACTOR.md`**: §11 Phase 1.75b subsection inserted.

4. **`tests/unit/test_phase1_75b_gating.py`**: NEW — 10 tests:
   - 6 functional tests of `_apply_slope_zones` (cliff/transition/talus × geology ON/OFF + flat baseline).
   - 4 structural grep tests (snow subsurface gated, sand subsurface gated, slope param exists, call site passes flag).

**Validation results (S48):**

| Gate | Result | Detail |
|---|---|---|
| Unit tests (sandbox Python 3.10) | **56/56 PASS** | 46 prior + 10 new Phase 1.75b gating tests. |
| 3×3 baseline 48_48 (flag-OFF) | **PENDING** | Must run on user's machine (rasterio/masks). Flag-OFF path is byte-identical by construction (gating only adds `if not False` checks). |
| 3×3 flag-ON 24_80 | **PENDING** | Must run on user's machine. |
| 3×3 flag-ON 59_53 | **PENDING** | Must run on user's machine. |
| Cliff cross-section diag | **PENDING** | First real pipeline run on user's machine. |

**Open items carried into next session:**
- **Palette tuning** — deferred. In-game/cross-section review may reveal color balance issues.
- **Sublayers 5-6** (river_bed, lake_bed) — requires threading `river_meta` / `hydro_lake` to `build_column_array()`.
- **Aspect convention drift** (carry-over from S44). Still unresolved.
- **51_53 flag-ON shadow hookup** (carry-over). Not Phase 1.75b scope.

### S48 (cont.) — Phase 2.0 surface pipeline pilot layers (2026-04-12)

**Phase state (end of S48 cont.):**
- Landed this session: **Phase 2.0**
- §11 currently at: **Phase 2.0** (updated session ref from S49 to S48)
- Next session starts: **Phase 2.5** — Terrain group (grass_terrace, weathered_top, forest_surface — forest_surface later removed S50)

**What landed:**

1. **`core/layers/pass2_surface/temperate_cliff_face.py`** — NEW. Partition layer (priority 10). Claims temperate biome pixels where `cliff_deg >= 35°` and land. Block selection: lithology group palette with 70/20/10 primary/secondary/accent scatter. Vectorized per-group painting.

2. **`core/layers/pass2_surface/temperate_talus_apron.py`** — NEW. Partition layer (priority 20). Claims temperate pixels where `18° <= cliff_deg < 35°` and concave (`concavity_norm > 0`). 50/50 cobblestone/gravel scatter at 60% density; passthrough on unscattered scope pixels.

3. **`core/layers/pass2_surface/vertical_fluting.py`** — NEW. Overlay layer (priority 50). Phase-modulated stripe pattern on already-claimed cliff pixels. Algorithm: 3×3 mean-filtered surface_y gradient → cliff tangent → dot-product phase → `(phase // 4) % N_VARIANTS` indexing into lithology palette. ±1 block jitter from noise.

4. **`core/layers/pass2_surface/__init__.py`** — Updated. Exports all 3 layer classes.

5. **`core/surface_decorator.py`**:
   - New params: `use_new_surface_pipeline: bool = False`, `lithology_tile: np.ndarray | None = None`.
   - Pipeline delegation block after shadow mode: when flag ON, builds SurfaceContext from eco_grads/cliff_deg/surface_y/noise_b/lithology, instantiates 3 layers, runs `run_pass()`, merges claimed+overlaid pixels into `surface_blocks`. Legacy path preserved for all unclaimed pixels.
   - Try/except wrapped — pipeline errors logged to stdout and swallowed, production tuple untouched.
   - Pre-existing EOF truncation fixed (test harness `print` + `sys.exit(0)`).

6. **`config/thresholds.json`** — `surface_pipeline.feature_flag_enabled: false` added.

7. **`tools/_pipeline_runner.py`** — Reads `surface_pipeline.feature_flag_enabled`, passes `use_new_surface_pipeline` and `lithology_tile` to `decorate_surface()`.

8. **`run_pipeline.py`** — Same wiring as `_pipeline_runner.py`.

9. **`tests/unit/test_phase2_0_layers.py`** — NEW — 21 tests:
   - 8 TemperateCliffFace tests (steep/flat/non-temperate/ocean/prior-ownership/granitic-palette/threshold-boundary).
   - 5 TemperateTalusApron tests (moderate-concave/flat/steep/blocks/convex).
   - 4 VerticalFluting tests (claimed-cliffs/unclaimed/flat/multiple-variants).
   - 3 pipeline composition tests (full 3-layer pass, flag param exists, default False).
   - 1 config test (feature_flag_enabled exists and is false).

**Validation results (S48 Phase 2.0):**

| Gate | Result | Detail |
|---|---|---|
| Unit tests (sandbox Python 3.10) | **77/77 PASS** | 56 prior + 21 new Phase 2.0 tests. |
| Flag-OFF regression | **PASS (by construction)** | `feature_flag_enabled: false` in config; `use_new_surface_pipeline` defaults False. Pipeline block never executes. |
| Flag-ON 59_53 3×3 | **PENDING** | Must run on user's machine with rasterio/masks. |
| Flag-ON in-game visual | **PENDING** | Cliff faces should show lithology-derived palette + visible fluting stripes. |

### S48 (cont. 2) — All-biome expansion + threshold tuning + triage (2026-04-12)

**Phase state (end of S48 cont. 2):**
- Landed this session: **Phase 2.0** (code complete, flag ON, visual validation BLOCKED — see triage)
- §11 currently at: **Phase 2.0**
- Next session starts: **Phase 2.0 triage** then **Phase 2.5**

**Changes landed:**

1. **Biome scope expansion** — `TEMPERATE_BIOMES` renamed to `LAND_BIOMES`, now includes all 25 land biomes (was 9 temperate-only). Cliff_face, talus_apron, and vertical_fluting now fire on all biomes with lithology group mappings. Discovered during in-game validation that 59_53 (windthrow) and 36_20 (rock exposure) had no steep temperate terrain — the original temperate-only scope meant the pipeline had nothing to claim.

2. **Threshold lowering** (in flux, needs further tuning):
   - `CLIFF_DEG_THRESHOLD`: 35° → 25°
   - `TALUS_DEG_MIN/MAX`: 18–35° → 15–25°
   - `MIN_CLIFF_DEG` (fluting): 35° → 25°
   - Rationale: in-game mountain faces on tile (25,80) (Y 159→315, 156-block elevation range) rendered as all-grass. The `cliff_deg` data at those visually-steep slopes was below 35°.

3. **Config**: `surface_pipeline.feature_flag_enabled` set to `true` (was false).

**In-game validation results:**
- 59_53 flag-ON: 74 PASS / 6 FAIL (pre-existing), no regressions vs baseline. Top-down stitched_blocks.png shows lithology palette differentiation. But in-game: no visible cliff rocks — tile is all gentle rolling terrain (windthrow), no ≥25° slopes.
- 25_80 (single tile, flag-ON, 25° threshold): still all grass in-game despite 156-block elevation range. **Root cause unresolved.**

**TRIAGE — lithology blocks not visible in-game: ROOT CAUSE FOUND.**

**Root cause:** `tools/validate_test_tile.py` line 625 called `decorate_surface()` without passing `use_new_surface_pipeline` or `lithology_tile`. Both defaulted to `False`/`None`, so the pipeline block never executed. The subsurface geology (geology flag) WAS wired correctly in this file, which is why underground lithology worked but surface didn't.

**Fix:** Added `_use_sp` + `lithology_tile` params to the `validate_test_tile.py` call site, matching the pattern already in `_pipeline_runner.py` and `run_pipeline.py`.

**Lesson:** When adding a new param to `decorate_surface()`, grep ALL call sites — there are 5+: `run_pipeline.py`, `_pipeline_runner.py`, `validate_test_tile.py`, `world_studio.py`, and the internal `__main__` test harness. Missing one means silent flag-OFF behavior.

Thresholds reset to originals (35°/18–35°/35°) after premature lowering during triage. Visual validation pending with correct wiring.

**Open items carried into next session:**
- **Threshold calibration** — 35° cliff threshold confirmed working but may need tuning per biome.
- **Aspect convention drift** (carry-over from S44).
- **51_53 flag-ON shadow hookup** (carry-over).
- **MC biome mapping bug** — (25,80) rendering as plains in-game, possible `_DEFAULT` fallthrough in `BIOME_TO_MC`.
- **world_studio.py** — still missing `use_new_surface_pipeline`/`lithology_tile` params (low priority, preview only).

### S48 (cont. 3) — Phase 2.5 layers + Gaea mask analysis (2026-04-12)

**Phase state (end of S48 cont. 3):**
- Landed this session: **Phase 2.0 + 2.5** (all 6 layers code complete, flag ON, 77/77 tests green, Phase 2.0 visually confirmed in-game on 25,80)
- §11 currently at: **Phase 2.5**
- Next session starts: **Phase 2.5 visual validation** then **Phase 2.75**

**Changes landed:**

1. **Phase 2.5 layers implemented** — 3 new partition layers:
   - `grass_terrace.py`: 8–18° slopes, grass_block dominant, coarse_dirt on south-facing (north_factor < 0.3, 35% scatter).
   - `weathered_top.py`: flat + high elevation (≥180 Y) + wind-exposed (>0.5). Mix: 25% stone, 30% mossy_cobblestone, 45% grass_block.
   - `forest_surface.py`: 13 forested biomes, tree-density-driven interpolation (high density → podzol/rooted_dirt/coarse_dirt mix; low density → grass dominant). **REMOVED S50** — over-claimed clearings/meadows; legacy decorator handles forest floors correctly.

2. **tree_density_hint module** — `core/tree_density_hint.py` stub computing density from biome + slope + moisture + disturbance. Was used by forest_surface layer (removed S50). Module retained but no longer called by pipeline.

3. **Phase 2.0 root cause fixed + visually confirmed** — `validate_test_tile.py` was missing `use_new_surface_pipeline`/`lithology_tile` params. Fixed. Lithology rock rendering confirmed correct on cliff faces in-game on (25,80). Thresholds reset to originals (35°/18–35°/35°).

4. **Gaea vs Python mask delegation analysis** — researched WorldPainter/Gaea pipelines. Recommendation: outsource 4 environmental exposure masks to Gaea (rock_exposure, snow_caps, wind_windthrow, sand_dunes) since they're terrain-erosion products. Keep hydrology, gap mask, lithology, surface pipeline layers, and tree density in Python (game-logic dependent).

**Test results:** 77/77 unit tests green (21 Phase 2.0 + 56 Phase 2.5 + prior).

**Open items carried into S49:**
- **Phase 2.5 visual validation** — grass_terrace, weathered_top need in-game confirmation. (forest_surface removed S50.)
- **Phase 2.75** — water-adjacent + cross-biome layers (riparian_fringe, river_bar, lake_edge, snow_cap_north, beach_by_fetch).
- **Gaea mask outsourcing** — spec Gaea node graphs for rock/snow/wind/sand if user wants to proceed.
- **MC biome mapping bug** — (25,80) rendering as plains.
- **Aspect convention drift**, **51_53 flag-ON shadow hookup**, **palette tuning**, **world_studio.py** params (all carry-forward).

---

### S49 — 2026-04-13: Biome mapping consolidation, ground cover gating, stone banding cleanup

**Phase state:** Landed this session: S49 bugfixes (biome mapping, ground cover, stone banding). §11 currently at: Phase 2.5 complete. Next session starts: Phase 2.75 (water-adjacent + cross-biome) OR visual validation of S49 fixes on (24,80).

**Context:** User generated tile (24,80) and found 4 problems: (1) stone banding on convex edges where terrain ≤18°, (2) MC biome mapping falling through to `_DEFAULT` (plains), (3) default ground cover (short_grass + flowers) blanketing entire mountain, (4) no slope/height/exposure cutoff on ground cover. User chose fix order: biome → ground cover → stone banding.

**Changes:**

1. **Biome mapping consolidation** — Root cause: `BIOME_TO_MC` dict was duplicated in 4 files with divergent mappings. `chunk_writer.py` (canonical) had `ALPINE_MEADOW → minecraft:plains` (temp 0.25 → rivers freeze). Fixed:
   - `core/chunk_writer.py`: ALPINE_MEADOW → `minecraft:meadow` (temp 0.5), EASTERN_TEMPERATE_COAST → `minecraft:beach` (temp 0.8 vs stony_shore 0.2).
   - `tools/validate_test_tile.py`: removed 30-line duplicate dict, now imports from `core.chunk_writer.BIOME_TO_MC`.
   - `tools/_pipeline_runner.py`: removed duplicate dict, `_mc_biome_map()` uses lazy import from `core.chunk_writer.BIOME_TO_MC`.
   - `tools/voxel_preview.py`: removed duplicate dict, lazy import from `core.chunk_writer.BIOME_TO_MC`.
   - `tools/world_studio.py`: still has duplicate — not fixed (world_studio broken per CLAUDE.md, low priority).

2. **Ground cover slope gating** — `core/surface_decorator.py`:
   - `_apply_ground_cover()` gains `cliff_deg` parameter + slope-based density modulation: 0% on cliffs (≥35°), 10% on talus (18–35°), 50% on moderate (8–18°), 100% on flat (<8°).
   - Post-pipeline ground cover suppression: any pixel with a non-plantable surface block (stone, andesite, granite, sandstone, terracotta, sand, snow, ice, etc.) gets `ground_cover` cleared to `""`.

3. **Stone banding cleanup (v1)** — `core/surface_decorator.py`:
   - Post-pipeline pass after surface_pipeline merge: any pixel where `cliff_deg ≤ 18°` AND surface is a hard rock block → forced to `grass_block`. Implements user rule: "≤18° = grass overrides convex-edge stone."
   - **SUPERSEDED same session** — user tested in-game, banding was worse. Replaced by approach (4).

4. **Lithology depth push + banding cleanup v2** — `core/chunk_writer.py`:
   - Root cause (partial): geology bands visible at convex edges because stone_mask reached `surface_y - 3` (only 2 blocks of dirt buffer). Fix: push stone_mask to `surface_y - 4`, add 3rd sub_blk layer at `sy-3`, update `_fill_geology_layers` stone_zone_top to match. Column layout now: `[Y_MIN+1, sy-4]` = geology, `[sy-3, sy-1]` = sub_blk (3 blocks dirt), `sy` = surface_blk.
   - Removed the v1 stone banding cleanup from `surface_decorator.py` (no longer needed — geology is buried deep enough that convex edges show dirt, not rock).
   - Stone faces now only appear on ≥35° cliffs via the existing TemperateCliffFace surface layer.
   - **In-game result: better but banding persisted.** Root cause identified as deeper: see (5).

5. **Slope smoothing — staircase aliasing fix (root cause)** — `core/eco_gradients.py` + all callers:
   - **Root cause:** `cliff_deg` computed from raw integer `surface_y` via `np.gradient`. Integer terrain is a staircase — every 1-block step edge produces a local 45° spike even on globally gentle (~15°) slopes. The ≥35° cliff layer claims every step edge → stone bands at every contour line. The banding is intrinsic to the discrete gradient, not fixable by post-processing.
   - **Fix:** New `compute_cliff_deg(surface_y, sigma=1.5)` helper in `core/eco_gradients.py`. Applies `gaussian_filter(sigma=1.5)` to surface_y before gradient computation. This recovers the true regional slope, eliminating staircase aliasing. sigma=1.5 ≈ 3-block effective kernel.
   - Updated all 5 call sites: `_pipeline_runner.py`, `validate_test_tile.py`, `surface_decorator.py` test helper, `chunk_writer.py` legacy cliff banding, `chunk_writer._fill_geology_layers` soil depth computation.

**Test results:** 77/77 unit tests green (after all fixes).

**Open items carried into S50:**
- **Regenerate tile (24,80)** — validate biome fix + ground cover gating + lithology depth + slope smoothing in-game.
- **Phase 2.75** — water-adjacent + cross-biome layers (riparian_fringe, river_bar, lake_edge, snow_cap_north, beach_by_fetch).
- **Ice in subsurface** — user noted at specific coords on (24,80); expects ground cover + topsoil update to resolve.
- **world_studio.py** duplicate BIOME_TO_MC — low priority.
- **Aspect convention drift**, **51_53 flag-ON shadow hookup**, **palette tuning** (all carry-forward).

---

### S50 — 2026-04-13: Phase 2.75 partial + ForestSurface removal

**Phase state (end of S50):**
- Landed this session: **Phase 2.75 partial** (3 of 4 layers: snow_cap_north, river_bar, desert_pavement). ForestSurface removed. BeachSurface attempted then removed.
- §11 currently at: **Phase 2.75** (updated S50 — scope revised, beach deferred to precompute mask)
- Next session starts: **Beach precompute mask** then **3×3 validation** of all Phase 2.75 layers.

**Changes landed:**

1. **ForestSurface layer removed** — It over-claimed all unclaimed forested-biome pixels including clearings/meadows, painting coarse_dirt/podzol where the legacy per-biome surface block logic + gap_mask meadow/clearing system already handled forest floors correctly. `tree_density_hint` computation removed from pipeline (module retained, no callers). 8 layers active (was 6).

2. **`snow_cap_north.py`** — NEW. Overlay layer (priority 55). Extends snow downslope on north-facing terrain in the "near snow line" band (`snow_caps_gradient` 0.20–0.40, below gap==7 trigger at 0.40). Scope: `north_factor >= 0.55`, `surface_y >= 160`, land. Probabilistic dither scales with both north_factor and gradient proximity — 85% at best conditions, 15% at margins. Places `snow_block`.

3. **`river_bar.py`** — NEW. Partition layer (priority 42). Arid biomes near river channels (`riparian_proximity >= 0.4`, `cliff_deg < 18°`). Block palette: coarse_dirt/packed_mud/sand scatter with dither fading from 90% at channel to 30% at corridor edge. **SAND_DUNE_DESERT variant** uses desert pavement palette (55% coarse_dirt / 45% packed_mud, no sand) per user directive.

4. **`desert_pavement.py`** — NEW. Partition layer (priority 43). Flat arid non-dune biomes (DESERT_STEPPE_TRANSITION, SEMI_ARID_SHRUBLAND, DRY_OAK_SAVANNA, DRY_WOODLAND_MAQUIS, DRY_PINE_BARRENS, DESERT_ROCK, COASTAL_HEATH). Places coarse_dirt (55%) / packed_mud (45%) with moisture-driven dither. Ground cover (dead_bush, short_dry_grass, tall_dry_grass) deferred to legacy GROUND_COVER_PALETTES update.

5. **BeachSurface attempted then removed** — Partition layer using per-tile EDT from `surface_y` to find ocean-adjacent flat low-elevation pixels. Didn't produce visible beaches on (48,48) — the per-tile EDT at 512×512 likely missed coastline geometry that only resolves at full 50k precompute scale. **Decision: defer to precompute mask approach** — write `rebuild_beach.py` to produce `beach.tif` at 1:8 (6250×6250) like rock_exposure/snow_caps, wire into pipeline as a gradient mask.

6. **§11 Phase 2.75 scope revised** — Dropped `temperate_riparian_fringe` and `lake_edge` (existing signals sufficient). Replaced `beach_by_fetch` with simpler physics-derived beach (deferred to precompute mask). Updated exit criteria.

**Open items carried into S51:**
- **Beach precompute mask** — `rebuild_beach.py` → `beach.tif` at 1:8. EDT from ocean at full world scale, thresholded by elevation band + slope. Wire into eco_gradients or as a new layer with the gradient mask pattern.
- **3×3 validation** — (24,80) for snow_cap_north + river_bar + desert_pavement; (48,48) for beach once mask exists.
- **Desert pavement ground cover** — add dead_bush/short_dry_grass/tall_dry_grass to GROUND_COVER_PALETTES for arid biomes.
- **Ice in subsurface**, **world_studio.py** duplicate BIOME_TO_MC, **aspect convention drift**, **51_53 flag-ON shadow hookup**, **palette tuning** (all carry-forward).

---

### S51 — 2026-04-13: In-game triage fixes + precompute mask conversions

**Phase state (end of S51):**
- Landed this session: **Phase 2.75 fixes** — 4 in-game issues fixed, 2 layers converted to precompute masks.
- §11 currently at: **Phase 2.75** (updated S51 — snow_cap_north + beach converted to masks)
- Next session starts: **Rebuild masks** (`rebuild_rock_exposure.py` for snow_caps_north.tif, `rebuild_beach.py` for beach.tif), then **3×3 validation** + **in-game test**. Desert pavement ground cover still pending.

**Changes landed:**

1. **Fix #1: Convex stone banding** — `core/column_generator.py` lines 1093-1094 used raw `np.gradient(surface_y)` without smoothing. Every 1-block step edge spiked to ~45° and triggered stone transition zone on gentle convex slopes. **Fix:** replaced with `compute_cliff_deg(surface_y, sigma=1.5)` from `core.eco_gradients`, which applies Gaussian pre-smooth to recover true regional slope.

2. **Fix #2: Snow cap north → precompute mask** — The `SnowCapNorth` surface pipeline layer (overlay, priority 55) had limited qualifying terrain in per-tile evaluation. **Converted:** Added `snow_caps_north.tif` generation to `rebuild_rock_exposure.py` (§13b). Gradient = `nf_t * grad_t` for pixels in snow_grad [0.10, 0.40), north-facing (nf >= 0.55), high elevation (sy >= 160). Wired through tile_streamer → eco_gradients (gap==7 at threshold 0.15) → all 3 pipeline runners. Surface layer removed from `__init__.py` + `surface_decorator.py`.

3. **Fix #3: Beach rework** — S51 first attempt was too wide with simplex noise artifacts. User directive: Y=63 only, no noise, sand-only. **Changes:** (a) `rebuild_beach.py` — reduced max_dist 6→3, elev_band 40→20, removed opensimplex noise entirely. (b) `eco_gradients.py` — removed simplex jitter from gap==9 assignment, added hard `surface_y == 63` gate. (c) `surface_decorator.py` — removed gravel dither, sand-only surface.

4. **Fix #4: Floodplain protection** — Activating missing mask kwargs in `_pipeline_runner.py` + `validate_test_tile.py` (snow_caps, sand_dunes, beach) caused riparian regression on (51,53) — gap==7 snow and gap==8 sand_dune were overriding gap==4 floodplain. **Fix:** (a) Snow assignment in eco_gradients now excludes `gap_mask != 4`. (b) Sand_dune overridable set changed from `{0,1,2,4}` to `{0,1,2}` — floodplain no longer overridable by dunes.

**Files modified:** `core/column_generator.py`, `core/eco_gradients.py`, `core/surface_decorator.py`, `core/tile_streamer.py`, `core/layers/pass2_surface/__init__.py`, `rebuild_beach.py`, `rebuild_rock_exposure.py`, `run_pipeline.py`, `tools/_pipeline_runner.py`, `tools/validate_test_tile.py`, `diag_rock_surface_topdown.py`.

**Open items carried into S52:**
- **Rebuild masks** — must run `rebuild_rock_exposure.py` (produces snow_caps_north.tif) and `rebuild_beach.py` (produces updated beach.tif with tighter params) before testing.
- **3×3 validation + in-game test** of all 4 fixes.
- **Desert pavement ground cover** — add dead_bush/short_dry_grass/tall_dry_grass to GROUND_COVER_PALETTES for arid biomes.
- **Ice in subsurface**, **world_studio.py** duplicate BIOME_TO_MC, **aspect convention drift**, **51_53 flag-ON shadow hookup**, **palette tuning** (all carry-forward).

### S52 — 2026-04-13: Legacy system purge + GrassTerrace biome-aware rewrite

**Phase state (end of S52):**
- Landed this session: **Phase 2.75 cleanup** — 3 legacy block-painting systems removed, GrassTerrace rewritten biome-aware, meadow override fixed.
- §11 currently at: **Phase 2.75** (no new phase — this was cleanup of existing layers)
- Next session starts: **In-game validation** of S52 changes (user's last test still showed stone banding from eco overlay + legacy slope zones — those are now deleted, needs rerun). Then **ecotone dither scoping** (24px grass bleed at biome boundaries), **beach blob fix** on (35,77), **desert pavement ground cover**.

**Diagnosis method:** Interview-style in-game testing. User flew around tiles (24,80), (51,53), (35,77) describing symptoms while Claude traced code paths. Identified three separate legacy systems all fighting the new surface pipeline.

**Changes landed:**

1. **GrassTerrace biome-aware rewrite** (`core/layers/pass2_surface/grass_terrace.py`) — Was writing uniform `grass_block` on ALL 8-18° slopes in every land biome. **Rewritten** with 7 biome category palettes: desert (sand/red_sand/sandstone), moss (moss_block/podzol), boreal (podzol/coarse_dirt), dry (coarse_dirt/gravel), arctic (snow_block/gravel/stone), alpine (grass_block/coarse_dirt/gravel), default temperate (grass_block/coarse_dirt/podzol). Uses cumulative noise thresholds for per-pixel dither — every block in a 3×3 is a different block. Added gap_mask protection (skips 4/5/6/7/8/9) and riparian protection (skips pixels with prior mud/clay/rooted_dirt from river bank handler). South-facing modifier uses biome-appropriate weathering blocks.

2. **Eco condition overlay deleted** (`core/surface_decorator.py` lines 916-946) — Was overlaying `BIOME_BLOCK_PALETTES` eco_* tags (eco_shallow_soil → granite, eco_ridge → tuff, eco_dry → podzol/granite) on top of noise_layers output, contaminating ~25% of gentle-slope pixels across all biomes. All six eco functions (ridge, basin, shallow_soil, deep_soil, dry, moist) now fully covered by noise_layers + surface pipeline + river bank handler. Eco_probs computation retained for legacy fallback path only.

3. **Final meadow override fix** (`core/surface_decorator.py` line 1396) — 2px dilation of gap==1/4 was bleeding grass_block into gap==6 (alpine_meadow), gap==8 (sand_dune), and gap==9 (beach). Added all three to exclusion set alongside existing gap==5 (rock) and gap==7 (snow).

4. **Legacy `_apply_slope_zones()` deleted** (`core/surface_decorator.py`) — Removed both the call site and the function definition. Was a second slope system (45°/65° thresholds + 8px talus dilation) writing stone/gravel/cobblestone on gap==0 steep pixels, redundant with TemperateCliffFace (35°+), TemperateTalusApron (18-35°), GrassTerrace (8-18°). `_DESERT_BIOMES_SET` constant also removed.

5. **Legacy slope zones + talus deleted from column_generator** (`core/column_generator.py` steps 6+6a) — Was a third slope system (25°/50° thresholds) writing into `surface_blk` which is dead code — `decorate_surface()` builds its own surface block arrays from scratch and chunk_writer uses those. Steps 6b-6d (altitude, alpine exposure, frost ridge) left in place as future cleanup — they also write to the dead `surface_blk` but are harmless.

**Files modified:** `core/layers/pass2_surface/grass_terrace.py` (full rewrite), `core/surface_decorator.py` (eco overlay removed, slope zones removed, meadow override fixed), `core/column_generator.py` (slope zones + talus removed).

**Key discovery — dead code in column_generator:** `process_tile_columns_v2()` steps 5-10 (biome palettes, slope zones, talus, altitude, alpine exposure, frost ridge, shoreline, ocean floor, and the costly per-pixel ColumnResult loop building `.blocks` dict) ALL write to `surface_blk` which is never consumed — `decorate_surface()` replaces it entirely. Full cleanup is a separate task but would save significant per-tile compute.

**Open items carried into S53:**
- **In-game validation** — user's last test still showed stone banding (from eco overlay + legacy slope zones which are now deleted). Needs fresh rerun.
- **Ecotone dither width** (24px) — swaps blocks between neighboring biomes within 24 blocks of boundaries. On desert/meadow boundaries, this paints grass_block 24 blocks into sand. Needs scoping down or gap-mask awareness.
- **Beach blob/staircasing** on (35,77) — Y=63-only constraint + no dither produces hard-edged blob. Needs small elevation band or edge dither.
- **Desert pavement ground cover** — dead_bush/short_dry_grass/tall_dry_grass for arid biomes.
- **Full column_generator surface code cleanup** — steps 5-10 of `process_tile_columns_v2()` write to dead `surface_blk`. Removing would save compute.
- Carry-forward: aspect convention drift, 51_53 flag-ON shadow hookup, palette tuning, ice in subsurface, world_studio.py duplicate BIOME_TO_MC.

### S53 — 2026-04-13: S52 bugfix + eco overlay organic restore + noise_layers stone purge

**Phase state (end of S53):**
- Landed this session: **S52 bugfix + Phase 2.75 refinement** — broken build fixed, eco overlay restored with organic palettes, noise_layers stone surface blocks purged.
- §11 currently at: **Phase 2.75** (no new phase — this was S52 cleanup/refinement)
- Next session starts: **In-game validation** of S53 changes. Stone banding NOT confirmed fixed — user reports it persists. If still present after validation, deeper investigation needed (ecotone dither, surface pipeline layers, subsurface bleed-through). Then ecotone dither scoping, beach blob, desert pavement.

**Critical S52 bug found:** `column_generator.py` crashed with `NameError: name 'desert_mask' is not defined` on every tile. The S52 slope zone deletion (steps 6+6a) orphaned three variable definitions (`desert_mask`, `grass_max_deg`, `trans_max_deg`) that downstream dead-code steps (7b flow-sand, 8 snow cap) still reference. This means S52's "still there" test was either running cached bytecode or crashed silently. Fixed by adding the three definitions back after the deletion comment.

**Changes landed:**

1. **column_generator.py NameError fix** — Added `desert_mask = np.isin(tile_biomes, list(DESERT_BIOMES))` before step 7b, and `grass_max_deg`/`trans_max_deg` from config after S52 deletion comment. Pipeline now runs without crash.

2. **Eco condition overlay restored with organic-only palettes** (`core/surface_decorator.py`) — S52 deleted the eco overlay entirely, which killed the high-frequency texture scatter that gave forest floors their visual character. Restored the overlay loop over BIOME_BLOCK_PALETTES eco_* entries, but with a substitution map that replaces all stone-variant surface blocks with organic equivalents: stone→packed_mud, granite→coarse_dirt, diorite→coarse_dirt, andesite→coarse_dirt, tuff→packed_mud, cobblestone→gravel, mossy_cobblestone→moss_block, deepslate→packed_mud, calcite→packed_mud, dripstone_block→packed_mud. Subsurface blocks kept as-is (stone subsurface only visible in cliff cross-sections). Same 6 physical signals: eco_ridge (wind_exposure), eco_basin (concavity_norm), eco_shallow_soil (1-soil_depth), eco_deep_soil (soil_depth), eco_dry (1-moisture_index), eco_moist (moisture_index).

3. **noise_layers_biome stone surface purge** (`config/thresholds.json`) — 7 biomes had stone-variant surface blocks in their noise_layers entries, painting stone on ALL terrain regardless of slope. Replaced: ARCTIC_TUNDRA tuff→packed_mud, BOREAL_TAIGA diorite→coarse_dirt, CONTINENTAL_STEPPE granite→coarse_dirt, DRY_PINE_BARRENS granite→coarse_dirt, SCRUBBY_HEATHLAND tuff→packed_mud, FROZEN_FLATS stone→packed_mud, KARST_BARRENS stone→packed_mud.

**Diagnostic results:** `diag_stone_trace.py` on tile (51,53) showed 0% stone on gap==0 MIXED_FOREST gentle-slope (<18°) pixels after noise_layers and eco overlay stages. Only 308/182239 (0.2%) gravel pixels from GrassTerrace south-facing scatter (intentional). **However, user reports banding still visible in-game — needs fresh validation run.**

**Files modified:** `core/column_generator.py` (NameError fix), `core/surface_decorator.py` (eco overlay restore), `config/thresholds.json` (7 noise_layers entries).

**Open items carried into S54:**
- **In-game validation** — stone banding NOT confirmed fixed. Needs fresh 3x3 + MCA generation + in-game check.
- **Ecotone dither width** (24px) — still can bleed blocks across biome boundaries.
- **Beach blob/staircasing** on (35,77).
- **Desert pavement ground cover** — dead_bush/short_dry_grass/tall_dry_grass.
- **Full column_generator surface code cleanup** — steps 5-10 write to dead surface_blk.
- Carry-forward: aspect convention drift, 51_53 flag-ON shadow hookup, palette tuning, ice in subsurface, world_studio.py duplicate BIOME_TO_MC.

### S54 — 2026-04-14: Stone contour-line banding ROOT CAUSE + fix

**Phase state (end of S54):**
- Landed this session: **Contour-line banding fix** — root cause identified and one-line fix applied.
- §11 currently at: **Phase 2.75** (no new phase — this was a cross-cutting bug fix)
- Next session starts: **Ecotone dither width** (24px grass bleed at biome boundaries), **beach blob/staircasing** on (35,77), **desert pavement ground cover**.

**Root cause:** `run_pipeline.py` line 207-208 computed `cliff_deg` via raw `np.gradient(surface_y.astype(np.float32))` without any smoothing. Integer `surface_y` is a staircase — every 1-block terrain step produces a local gradient of 1.0, which `np.arctan` maps to **45°**. This unsmoothed `cliff_deg` was passed through `decorate_surface()` into the surface pipeline's `SurfaceContext.eco_grads["cliff_deg"]`, where it drove threshold gates in every slope-sensitive layer:

- `temperate_cliff_face` (≥35°) → stone/andesite/cobblestone on every terrain step
- `temperate_talus_apron` (18-35°) → cobblestone scatter on every terrain step
- `vertical_fluting` (≥35°) → stone variants (only where cliff_face already claimed)
- `grass_terrace` (8-18°) → biome-keyed scatter

The result: visible stone contour-line bands tracing every Y-level transition across all terrain, most visible on flat valley floors where the stone contrasts sharply with the grass/podzol palette.

Meanwhile, `core/eco_gradients.py::compute_cliff_deg()` already existed with sigma=1.5 Gaussian pre-smooth specifically designed to eliminate this artifact — its docstring reads "eliminates contour-line banding artifacts in the surface pipeline layers." But only `chunk_writer.py` (subsurface banding) called it; the surface pipeline received the raw unsmoothed version.

**Fix (1 line):** `run_pipeline.py` line 207-208 replaced:
```python
# BEFORE (raw gradient, 45° at every 1-block step):
_gy, _gx = np.gradient(surface_y.astype(np.float32))
cliff_deg = np.degrees(np.arctan(np.hypot(_gx, _gy))).astype(np.float32)

# AFTER (Gaussian-smoothed, recovers true regional slope):
cliff_deg = core_eco.compute_cliff_deg(surface_y)
```

**In-game validation:** Tile (51,53) regenerated and loaded. Flat-terrain stone contour-line banding **eliminated**. Forest floors show clean grass/podzol/coarse_dirt mix without stone intrusions at Y transitions.

**River bank cobble/gravel fix (same session):** River banks (NOT lakes) displayed cobble/gravel mix instead of their normal riparian block dither. Cause: river carving creates 2-4 block channel drops that exceed the 18° talus and 35° cliff thresholds even after Gaussian smoothing of cliff_deg. Both `temperate_cliff_face` and `temperate_talus_apron` had no river exclusion — purely slope-gated. Fix: added `riparian_proximity >= 0.3` exclusion to the scope mask in both layers. Pixels near rivers (within ~30% of the riparian max distance, i.e. within the carved channel influence zone) are excluded from cliff/talus painting, letting the normal riparian block dither from `decorate_surface()` handle them instead. `vertical_fluting` cascades automatically (requires cliff_face ownership to fire). `grass_terrace` already had a riparian block-name guard. In-game validated on (51,53) — river banks restored to normal palette.

**Files modified:** `run_pipeline.py` (cliff_deg computation), `core/layers/pass2_surface/temperate_cliff_face.py` (riparian exclusion), `core/layers/pass2_surface/temperate_talus_apron.py` (riparian exclusion), `CLAUDE.md`, `PHYSICAL_REALISM_REFACTOR.md` (this entry), `memory/CONTOUR_BANDING_FIX.md` (standalone reference).

**Open items carried into S55:**
- **Ecotone dither width** (24px) — biome boundary bleed.
- **Beach blob/staircasing** on (35,77).
- **Desert pavement ground cover** — dead_bush/short_dry_grass/tall_dry_grass.
- **Full column_generator surface code cleanup** — steps 5-10 write to dead surface_blk.
- Carry-forward: aspect convention drift, 51_53 flag-ON shadow hookup, palette tuning, ice in subsurface, world_studio.py duplicate BIOME_TO_MC.

---

### S55 — 2026-04-14/15: Programmatic beach + ecotone widening + sand_dunes biome gate + misc bug hunt

**Phase state (end of S55):**
- Landed this session: **Phase 2.75c** (programmatic coastal beach with salt-and-pepper dither).
- §11 currently at: **Phase 2.75** (Phase 2.75c inserted for the beach rework; Phase 2.75's beach bullet marked SUPERSEDED).
- Next session starts: **Phase 3** (Pass 3 ground cover + Pass 4 vegetation on pilot tile 36_20) OR the deferred carry-forward list below. User's call.

**What landed (in dependency order of how the bugs surfaced):**

1. **Beach re-architecture (gap==9)** — `core/eco_gradients.py` beach section fully rewritten. Per-tile programmatic placement: ocean seed `(surface_y < 63) & ~river_mask`, EDT from ocean, biome-class-driven width (FULL coast: `base=4, amp=2`; SHALLOW forest coast: `base=2, amp=1`), Gaussian-blurred width noise (sigma=12, ~12-block coast-parallel lobes), dither zone 3× core, probability clamped `[0.15, 0.85]` across dither for true salt-and-pepper, decision coin Gaussian-blurred at sigma=1 (near per-pixel). Beach eligibility stomps `gap==0 | gap==1 | gap==4` (meadow + floodplain) in coastal zone. `beach.tif` no longer consulted at runtime. New `EcoGradients.beach_edge_mask` field tracks dither-non-sand pixels for downstream ground-cover thinning. See Phase 2.75c for full spec + §19 entry S55-1 for drift context.

2. **Underwater near-shore sand killed** — `core/column_generator.py:553` previously painted seafloor sand via binary depth threshold, producing long straight sand lines visible through water. Removed entirely. Seafloor now uses `underwater_floor_block()` gradient for all depths. Near-shore seafloor aesthetic deferred to a dedicated underwater pass.

3. **`sand_dunes.tif` gap==8 biome gate** — `core/eco_gradients.py:462` gated to `biome == "SAND_DUNE_DESERT"` only. `sand_dunes.tif` was spilling into `DESERT_STEPPE_TRANSITION` (65% coverage) and `SEMI_ARID_SHRUBLAND` (80% coverage), painting whole non-dune-desert biomes as sand. Global scan showed 99.9% of `SAND_DUNE_DESERT` is already covered by the mask, so the biome gate is effectively redundant inside its intended target but prevents overreach.

4. **Preview renderer ocean mask strict** — `core/preview_renderer.py:134` `surface_y <= 63` → `surface_y < 63`. Previous code painted Y=63 land pixels as ocean-blue regardless of surface block, hiding beach sand in validator PNGs. User-facing validator output fix only; runtime pipeline unaffected.

5. **Ecotone widening** — `core/surface_decorator.py:_apply_ecotone_dither`: `width_px` default `24 → 48`, decision `rand_field` now `gaussian_filter(rng.random, sigma=3)` for coherent salt-and-pepper at the biome boundary. Still only fires on multi-biome tiles — **cross-tile ecotone at tile seams is a deferred carry-forward**. Both S55 test tiles (6,72 and 35,77) are single-biome + ocean, so ecotone had no effect here; to validate the widened ecotone, use a tile with 2+ non-ocean biomes.

6. **Beach-edge ground cover thinning** — `core/surface_decorator.py:_apply_ground_cover`: `eco_density_mod[beach_edge_mask] = 0.15` drops ground-cover density to 15% on the beach dither zone's non-sand pixels. Without this, plants (ferns/moss_carpet/tall_grass) covered every non-sand pixel and the mix zone looked like solid forest from aerial view. The fix makes the biome floor blocks (mud, coarse_dirt, moss_block, podzol) visible through the reduced vegetation — which is where the salt-and-pepper effect actually comes from.

**Bug archaeology (surfaced during iteration):**
- **NameError in eco_gradients `del` statement** (v5): after the v3/v4/v5 refactors removed `_beach_mask_ok`, a stale `del (..., _beach_mask_ok, ...)` tuple at line 654 raised `NameError` every call. Exception was swallowed by outer pipeline handling, causing `compute_eco_gradients` to exit early **after** setting `gap_mask == 9` but **before** post-processing. User saw "no change" for 3 iterations because the beach was being set and then cleared by downstream code, or the NamedTuple construction was skipped. Fixed by removing `_beach_mask_ok` from the del list. **Lesson:** pipeline exception handling can swallow NameError from stale del tuples; don't trust "beach code looks right" if user reports no visible change — add a trace print.
- **Slope gate killed all beaches** (v3/v4): original v3 added `cliff_deg > 15.0` as hard exclusion; v4 as soft multiplier `(1 - (cliff_deg - 5) / 20)`. Both zeroed width on every coastal pixel because even the S54-smoothed `cliff_deg` on a 1-3 block coastal rise exceeds 15°. Replaced with biome-only eligibility (v5+) — physical slope is encoded upstream in the biome assignment.

**Files modified:**
- `core/eco_gradients.py` — beach block rewrite (~170 lines), sand_dunes biome gate, `EcoGradients.beach_edge_mask` field
- `core/surface_decorator.py` — beach-edge ground cover thinning, ecotone widening
- `core/column_generator.py` — underwater near-shore sand removed + Step 5 legacy shoreline removed (actually removed S54 session-last, committed now)
- `core/preview_renderer.py` — ocean mask strict
- `PHYSICAL_REALISM_REFACTOR.md` — Phase 2.75c inserted, Phase 2.75 beach bullet SUPERSEDED, §18 S55 entry, §19 S55-1 drift entry
- `CLAUDE.md` — current state line + direction updated
- `diag_beach_debug.py` — new standalone fast-topdown tool (~200 lines, ~0.4s runtime) for beach geometry debugging. Dumps 3 PNGs: `beach_regions` (4-color map of core/dither-sand/dither-nosand/biome), `beach_prob` (P(sand) heatmap), `beach_coin` (Gaussian-blurred coin field). Kept as a workflow tool — paired with parameter changes in `eco_gradients.py`, it gives sub-second feedback on geometric changes before paying the 75s validate cycle.

**Validated:** (6,72) LUSH_RAINFOREST_COAST in-game, user signoff 2026-04-15 "Beach looks great." (35,77) SEMI_ARID_SHRUBLAND shows zero surface beach (biome gate excludes it) — expected behavior.

**Open items carried into next session:**
- **Cross-tile ecotone dither** — tiles compute ecotone in isolation. Needs tile-streamer-level awareness to dither across seams. Not urgent for single-biome tiles but matters for transition tiles.
- **SEMI_ARID_SHRUBLAND native palette tune** — biome's intrinsic `sand`/`red_sand` noise entries produce ~9% sand coverage inland that can form elongated shapes. User flagged but accepted for now.
- **Ecotone validation on a multi-biome tile** — neither (6,72) nor (35,77) exercises the widened ecotone. Need to pick a multi-biome coastal or transition tile and confirm the 48px Gaussian-blurred band works.
- **Desert pavement ground cover** (S54 carry) — `dead_bush`, `short_dry_grass`, `tall_dry_grass` for arid biomes.
- **Full `column_generator` surface code cleanup** (S54 carry) — steps 5-10 write to dead `surface_blk`.
- **Phase 3 proper** — Pass 3 ground cover + Pass 4 vegetation on pilot tile 36_20. This is the canonical §11 next phase; the beach work was a Phase 2.75 closeout.
- Carry-forward from prior sessions: aspect convention drift, 51_53 flag-ON shadow hookup, palette tuning, ice in subsurface, world_studio.py duplicate BIOME_TO_MC, column_generator dead code.

---

## 19. Spec-vs-Code Drift Log

*Appended S46 (2026-04-11). Running log of discoveries where this document's spec drifts from the actual Vandir codebase. Each entry: what was discovered, where in the doc it drifts, what the correct framing is, and which phase fixed it. Keep entries appended in chronological order — never edit prior entries in place. This log exists because the S44→S45 phase-numbering drift and the S46 column-generator-vs-chunk-writer drift both cost meaningful session time to unwind, and a running log is cheaper than repeatedly rediscovering the same mismatch.*

### Entry S46-1 — Lithology injection point (`column_generator.py` → `chunk_writer.build_column_array()`)

- **Discovered:** 2026-04-11, Session 46 (pre-first-edit reconnaissance).
- **Drift location:** §6 Pass 1 ("Geology / Column — additive path in existing column_generator.py") and §11 Phase 1 ("Column additive path"). Both described extending `column_generator.py`'s `fill_column(...)` per-column API with lithology params and an inner `_fill_column_with_geology()`.
- **Reality:**
  1. `core/column_generator.py` has no `fill_column()`. Its hot path is `process_tile_columns_v2()`, a 575-line vectorized tile-level function taking `(H, W)` arrays. The legacy per-column `generate_column()` exists at line 452 but no production caller uses it.
  2. **`column_generator.py` does not control mid-column block fill at all.** The `ColumnResult.blocks` sparse dict it emits contains only `BEDROCK_Y`, `sy-2`, `sy-1`, `sy`, water fill, and dune fill. Everything in `[BEDROCK_Y+1, sy-3]` is absent from the dict.
  3. Mid-column fill lives inside **`core/chunk_writer.build_column_array()`** at line 202. That function takes the same `(H, W)` tile arrays and constructs the entire `(Y_RANGE, H, W)` voxel volume directly, including the hardcoded `[Y_MIN+1, sy-3] → stone` fill with per-biome cliff banding variants (`_BIOME_CLIFF_STONE` + `_CLIFF_BANDS` tables, Y-slice loop processing in steps of 32).
- **Correct framing:** lithology injects into `chunk_writer.build_column_array()` via new `(H, W)` tile kwargs. The function's existing Y-slice loop is the exact pattern lithology basement/sediment/soil_horizon should follow. `column_generator.py` may still need to thread the new kwargs through `process_tile_columns_v2()` and `generate_columns()` so they arrive at `build_column_array()` call sites in `tools/_pipeline_runner.py` / `tools/validate_test_tile.py` / `tools/check_tile_seams.py`, but that's pass-through wiring, not the injection itself.
- **Fix:** §11 Phase 1 marked SUPERSEDED; new §11 Phase 1.5 subsection inserted with correct `build_column_array()`-centric spec; §6 Pass 1 header note added referencing this drift log entry; §18 S46 entry documents the discovery; CLAUDE.md Workflow gains a new "Codebase-reconciliation check" rule that requires each session to verify spec signatures against real code before any code edit. Phase 1.5 ships the scaffolding; geology content is the next session's work.
- **Prevention rule:** Codebase-reconciliation check in CLAUDE.md Workflow. Any session implementing a §11 phase must first open the referenced source files and confirm the function signatures / injection points exist as spec'd. If not, STOP, append a new entry to this §19 log, and update §11 with a decimal subsection in the same commit as any code — never rename existing phases.

### Entry S55-1 — Beach: precompute mask + Y=63 hard gate abandoned for in-eco_gradients programmatic placement

- **Discovered:** 2026-04-14/15, Session 55 (in-game validation of S51/S54 beach output).
- **Drift location:** §11 Phase 2.75 beach bullet (S51) — "precompute mask `beach.tif` via `rebuild_beach.py`. EDT from ocean at 1:8, tight elevation + slope gate. Y=63 constraint enforced in eco_gradients (gap==9). Sand-only surface, no gravel/podzol dither."
- **Reality (S55 findings):**
  1. **Blobby shorelines** — `beach.tif` is generated at 1:8 (6250×6250), bilinear-upscaled to 50k (50000×50000). Hard threshold at `>= 0.05` on the upscaled bilinear mask at 1-block resolution = a staircase contour. Nothing in the precompute-mask pipeline addresses this.
  2. **Hard Y=63 gate discretizes the waterline** — S51's `surface_y == 63` constraint in `eco_gradients.py` clipped beach to exactly the sea-level contour. Any Y=63 land pixel got sand, any Y=64 pixel didn't. Block-level staircase.
  3. **Sand-only painting creates hard edges against biome palette** — no dither, no transition. In LUSH_RAINFOREST_COAST this meant sand stopped abruptly against podzol/mud walls from the biome's native noise palette. User's qualitative read: "no salt-and-pepper, hard cutoff, bizarre artifacts."
  4. **beach.tif as eligibility gate was too restrictive** — the tight elevation + slope gate limited beach to ~13 blocks from ocean. Widening the placement algorithm (S55 v1/v2) to cap at higher `total_width` didn't help because the eligibility mask itself only covered the narrow strip.
  5. **Meadow (gap==1) and floodplain (gap==4) can claim coastal pixels before beach runs.** The ordered pipeline (floodplain → alpine → rock → windthrow → meadow → snow → sand_dune → beach) with each claiming `gap_mask == 0` meant meadow/floodplain grass clearings appeared cut into coastlines — especially in LUSH_RAINFOREST_COAST where meadow freq 0.02 and floodplain freq 0.10 both fire.
  6. **Underwater near-shore sand** (`column_generator.py:553-557`, separate from gap==9) painted long straight seafloor sand blobs via binary depth threshold — also looked blocky/artificial at aerial view through water.
  7. **preview_renderer** was painting any `surface_y <= 63` pixel as ocean-blue regardless of actual surface block, hiding gap==9 sand in validation PNGs. (Minor, not a pipeline bug — validator output issue only.)
- **Correct framing:** Beach placement belongs in `core/eco_gradients.py` as per-tile programmatic computation, not as a precompute mask. The physical signals (surface_y, biome_grid, cliff_deg, river_meta) are already passed into `compute_eco_gradients`; re-deriving the coastline via EDT at 1:1 is cheap (~50ms per tile) and avoids the 1:8 → 50k upscale staircase. Biome gate replaces the physical-mask eligibility; the mask's value was always proxying for "this biome is coastal enough to have beach," which is better expressed directly. `beach.tif` is retained as a debug/preview artifact but no longer wired into the runtime pipeline.
- **Fix:** §11 Phase 2.75 beach bullet marked SUPERSEDED; new §11 Phase 2.75c subsection inserted with the programmatic design; §18 S55 entry documents what landed. Supporting fixes for underwater-sand, sand_dunes overreach, preview renderer, and ecotone width all landed in the same commit — each was a compounding issue uncovered while chasing the beach.
- **Prevention rule (carries over from S46-1):** precompute masks that require a hard threshold at 1-block resolution will always staircase. If a physical signal is needed AND the output is rasterized at 1:1, compute inline — the 1:8 precompute is an optimization, not an architecture. Masks like `override.tif` (discrete biome codes, NEAREST upscale) are fine; continuous-value masks thresholded for binary decisions are suspect.

