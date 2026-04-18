# PHYSICAL_REALISM_VEGETATION_REVAMP_ARCHIVE.md

**Status:** RETIRED in Session 59 (2026-04-17).

**Reason for retirement:** After Phase 3a (S57) and Phase 3b (S58) landed the in-scope ground cover + boundary appearance work inside the legacy `surface_decorator.py` + `schematic_placement.py`, the user reviewed the remaining Layer-Protocol vegetation revamp and concluded the existing systems were working well and did not warrant the full rewrite. The scope below was moved here for historical traceability and will not be executed as written. If future work reopens this direction, this file is the canonical starting point; it is NOT the current plan.

**What replaced it:**
- Ground cover logic stays in `core/surface_decorator.py:_apply_ground_cover` (S27+).
- Vegetation placement stays in `core/schematic_placement.py:place_schematics` (density-map + permutation + collision-grid, NOT Poisson-disk).
- S59 added ground cover ecotone dither (`_apply_ecotone_dither_ground_cover`) and schematic seam dither (per-candidate entries-list swap in `place_schematics`) — both re-use the same 30-block linear ramp + per-pixel salt-and-pepper geometry introduced for surface/subsurface in S58.

**Archived on 2026-04-17 at S59 end.** Retired sections reproduced verbatim below — do not treat as current.

---

## §6 Pass 3 — Ground Cover (RETIRED)

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

## §6 Pass 4 — Vegetation / Schematics (RETIRED)

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

## §11 Phase 3 — Temperate Mountain Pass 3 + 4 (RETIRED)

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

## §11 Phase 4 — Pilot decision gate (RETIRED)

**Deliverables:**
- In-game validation session: Nick + Claude review tile 36_20 against the north star reference images.
- Decision: roll out horizontally to other biome groups (Phase 5) OR pivot.

**Exit criteria:** Nick signs off on the pilot or names specific failures.

---

## §11 Phase 5 — Horizontal rollout (RETIRED)

**Deliverables per biome group:**
- Complete Pass 2 layer set (desert: `desert_cliff_face`, `desert_pavement`, `desert_vertical_fluting`, etc.; boreal: `boreal_cliff_face`, `boreal_moss_carpet`, etc.; coast: `beach_by_fetch` + marine variants).
- Pass 3 ground cover layers per biome.
- Pass 4 vegetation layers per biome.
- Tile-specific baselines: 24_80 (desert), 59_53 (windthrow/boreal), 25_72 (flat sand), 16_73 (meander).
- Old `surface_decorator.py` deleted after all biome groups pass.

**Exit criteria per biome group:** in-world comparison passes against the relevant north star reference image.

**Risk:** known. This is where the per-biome tuning debt lives. Pace: 1 biome group per week, realistic budget 3–5 weeks total.
