# Phase 3a Plan — Meadow Clearing System + Boreal Moss Modulation

## Context

Phase 2.75e landed (S57): surfaces are clean, noise-only palettes, no banding. The next visual leap is **forest clearings** — organic grass openings inside forested biomes driven by a shared low-frequency noise field (`meadow_clearing_field`). This is the single biggest missing feature for top-down realism: currently forests are uniformly dense, with no natural glades/meadows breaking up the canopy.

**Approach:** Wire the existing `meadow_clearing_field.py` (Phase 0, functional, not yet called) into the existing `_apply_ground_cover()` and `place_schematics()` systems. Add clearing-edge dither as a new concept. Keep per-biome palette system as-is. No Layer-protocol rewrite.

**Phase 3b (next session, separate plan):** Cross-tile ecotone awareness — biome transitions across tile seams get softened. Touches tile_streamer, run_pipeline, biome_assignment. Own session so it can be validated independently.

**Biome-within-tile edge softening:** Already exists and works — `_apply_ecotone_dither()` in `surface_decorator.py:1435`, called at line 1207. 48px sigmoid transition band, samples neighbour biome's actual blocks. No changes needed in this phase.

**Pilot tile:** (51,53) — rivers/lakes/mixed forest, existing 3×3 baseline.

---

## What the clearing system does

A single low-frequency noise field (`wavelength=256 blocks`, seed `0xBEEF_CAFE`) is precomputed once per tile. Both ground cover and tree placement read the same field:

- **Clearing interior** (field < 0.38): dense `short_grass`, near-zero tree density. No tall_grass (that's reserved for the clearing edge). Extremely rare flowers (1/500 grass blocks).
- **Clearing edge** (|field - 0.38| < 0.06): dither zone. Surface block shifts toward grass_block. Tree density drops to ~40% of interior. `tall_grass` band appears (3-4 blocks wide). Sparse tree scatter 2 blocks into clearing side.
- **Forest interior** (field > 0.44): existing behavior unchanged.

Floodplain corridors (gap==4) use the same field — natural tree-free stretches along rivers read as grass clearings.

---

## Implementation steps

### Step 1: Wire meadow_clearing_field into run_pipeline.py

**File:** `run_pipeline.py` (~line 210, after eco_gradients)

- Import `compute_meadow_clearing_field` from `core/meadow_clearing_field.py`
- Compute once: `clearing_field = compute_meadow_clearing_field(tile_x, tile_y, H=512, W=512)`
- Pass to `decorate_surface()` as new parameter
- Pass to `place_schematics()` as new parameter

### Step 2: Add clearing_field parameter to decorate_surface()

**File:** `core/surface_decorator.py`

- Add `clearing_field: np.ndarray | None = None` parameter to `decorate_surface()` signature (~line 766)
- Pass through to `_apply_ground_cover()` as new parameter

### Step 3: Integrate clearing field into _apply_ground_cover()

**File:** `core/surface_decorator.py`, `_apply_ground_cover()` (~line 1935)

Add clearing_field parameter. When provided:

1. **Compute clearing masks** using `clearing_interior_mask()` and `clearing_seam_mask()` helpers from `meadow_clearing_field.py`
2. **Biome gate:** Only apply to temperate forested biomes + floodplain corridors:
   - `CLEARING_BIOMES = {"TEMPERATE_RAINFOREST", "TEMPERATE_DECIDUOUS", "BOREAL_TAIGA", "MIXED_FOREST", "BIRCH_FOREST", "RIPARIAN_WOODLAND"}`
   - Also apply where `gap_mask == 4` (floodplain) regardless of biome
3. **Clearing interior pixels** (biome-gated & clearing_interior_mask):
   - Override ground cover to `short_grass` dominant (~85%), sparse `dandelion`/`poppy`/`oxeye_daisy` (0.2% each = ~1/500 grass blocks), trace `fern` (5%)
   - Set `eco_density_mod` to 1.3 (lush grass)
   - Suppress all shade-loving species (moss, leaf_litter, pale_moss_carpet → 0)
4. **Clearing edge pixels** (biome-gated & clearing_seam_mask):
   - Override ground cover to `tall_grass` dominant (~60%), `short_grass` (20%), `fern` (15%), sparse flowers (5%)
   - Set `eco_density_mod` to 1.1

### Step 4: Integrate clearing field into place_schematics()

**File:** `core/schematic_placement.py`

Add `clearing_field: np.ndarray | None = None` parameter to `place_schematics()`.

When provided:
1. Import clearing constants from `meadow_clearing_field.py`
2. **Clearing interior:** multiply tree placement density by 0.02 (near-zero — clearings are tree-free)
3. **Clearing edge:** multiply tree placement density by 0.4 (thinning zone — scattered trees spilling into clearing)
4. **Forest interior:** unchanged (field > 0.44)
5. **Floodplain gate:** same density suppression where `gap_mask == 4` AND clearing field indicates clearing

### Step 5: Surface block override in clearing interior + edge dither

**File:** `core/surface_decorator.py`, in `decorate_surface()` after block mixing (~after line 940), model after the existing gap==1 meadow override pattern (lines 963-970).

**Why this is needed:** Ground cover overrides (Step 3) only paint the top-decoration layer. Without also changing the surface block, clearings would have short_grass sitting on podzol/coarse_dirt — doesn't read as a meadow. The existing gap==1 (meadow) code already solves this problem for hydrology-driven clearings; we replicate the pattern for noise-driven forest clearings.

**Scope:** Biome in `CLEARING_BIOMES` AND clearing_field-relevant pixels. Keep off SNOWY_BOREAL_TAIGA/ARCTIC_TUNDRA/FROZEN_FLATS (not in CLEARING_BIOMES).

**Clearing INTERIOR** (clearing_interior_mask, field < 0.38):
- Convert forest-floor surface blocks to grass_block:
  - `podzol` → `grass_block` (all)
  - `dirt` → `grass_block` (all)
  - `coarse_dirt` → `grass_block` (90%, keep 10% as compacted patches — same ratio as meadow gap)
  - `moss_block` → `grass_block` (all) — moss doesn't belong in open meadows
  - `rooted_dirt` → `grass_block` (all)
  - Leave `gravel`/stone-variant blocks alone (rare, reads as rocky patches)

**Clearing EDGE** (clearing_seam_mask, |field - 0.38| < 0.06):
- Probabilistic shift: same block conversions as interior but with probability proportional to distance-through-seam (0 at outer edge → 1 at threshold).
- Creates a visible surface-block transition at the forest/clearing boundary paired with the tall_grass ground cover band.

**Order:** This block must run AFTER the noise_layers_biome block mixing (which sets podzol/etc.) and BEFORE the gap==1/==4 meadow/floodplain overrides. That way hydrology-driven meadows still stomp everything (as they do today), and noise-driven clearings stomp forest surface but yield to floodplain/meadow.

### Step 6: Update validate_test_tile.py / validate_3x3.py

Both validators call `decorate_surface()` and `place_schematics()` — need to pass `clearing_field` through. Add computation at the same point as run_pipeline.

### Step 7: Boreal moss carpet concavity modulation

**File:** `core/surface_decorator.py`, `_apply_ground_cover()` (~line 1935)

Per §6 spec: boreal biome moss density scales with `concavity_norm` — denser in basins/hollows, sparser on convex ridges.

1. **Scope:** `BOREAL_TAIGA` only. Apply concavity-driven density multiplier to `moss_carpet` and `pale_moss_carpet` species entries:
   - `moss_density_mult = 0.5 + 0.5 * eco_grads.concavity_norm`  (range [0.5, 1.0], boost in concave)
   - Scale per-species final density (`fd`) for these two blocks only
2. **Snow-biome exclusion (CRITICAL):** Remove `moss_carpet` and `pale_moss_carpet` from ground cover palettes for biomes where precipitation is snow:
   - `SNOWY_BOREAL_TAIGA` palette (line 359) — remove or zero out both
   - `ARCTIC_TUNDRA` palette (line 364) — verify not present (currently isn't)
   - `FROZEN_FLATS` palette — empty, nothing to do
   - **Why:** moss_carpet is a non-full-block plant carpet. Snow landing on a moss_carpet-covered pixel can't accumulate because the block above the moss isn't solid — MC's snow_layer requires a solid top face. Result = patchy/broken snow coverage in snowy biomes. This is a server-side tick behavior, not worldgen, so we prevent it at palette time.

Steps 3-4 (clearing logic) already suppress shade-loving species (moss) in clearing interiors, so no interaction issue.

---

## Files modified

| File | Change |
|------|--------|
| `run_pipeline.py` | Import + compute clearing_field; pass to decorate_surface + place_schematics |
| `core/surface_decorator.py` | New param on decorate_surface + _apply_ground_cover; clearing interior/edge/dither logic |
| `core/schematic_placement.py` | New param on place_schematics; clearing density suppression |
| `tools/validate_test_tile.py` | Pass clearing_field through |
| `tools/validate_3x3.py` | Pass clearing_field through |

## Files read (no changes)

| File | Purpose |
|------|---------|
| `core/meadow_clearing_field.py` | Already functional — compute_meadow_clearing_field(), clearing_interior_mask(), clearing_seam_mask(), constants |

---

## Verification

1. **Import check:** `python -c "from core.meadow_clearing_field import compute_meadow_clearing_field; print('OK')"`
2. **3×3 on (51,53)** with `--baseline tests/baselines/3x3/51_53` — confirm no PASS→FAIL regressions; inspect `stitched_blocks.png` for visible clearing patches in forest areas
3. **Single tile (51,53)** .mca → copy to Vandirtest10 → in-game check:
   - Forest clearings visible as grass openings
   - Clearings have organic blob shapes (~200 block wavelength)
   - Tall_grass band at clearing edges
   - Trees thin approaching clearings, absent in clearing interior
   - Floodplain corridors have matching tree-free stretches
   - No clearings in non-forested biomes (desert, tundra, etc.)

## What this does NOT include (deferred)

- **Phase 3b — Cross-tile ecotone** (next session, own plan). Makes biome transitions across tile seams get softened. Touches tile_streamer, run_pipeline, biome_assignment, ecotone function. User's top priority after 3a ships.
- Layer-protocol rewrite of ground cover
- Alpine flower field rarity system
- Per-biome symmetric edge recipes (e.g. asymmetric desert↔forest boundary behavior)
- Ground cover ecotone (only surface blocks are dithered at biome boundaries today; ground cover switches abruptly)
- Pass 4 Poisson-disk vegetation rewrite
- diag_suitability_field.py upgrade from stub to real
