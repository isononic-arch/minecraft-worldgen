# S88 Walk #13 Handoff — Rock-Layer SIMPLIFICATION before further refinement

**Branch tip:** `cd662b2` on `s85-cherry-picks`
**Date:** 2026-05-28 (session 88 closing, walk #13 not yet started)
**Test world:** Vandirtest10
**Active 6-tile pure-litho set:** (72,60) granitic / (24,80) arid_basaltic / (89,52) temperate_basaltic / (36,15) limestone / (19,44) deepslate / (64,72) mossy_temperate
**Coords:** see `cloud_bake/render_s88_litho_4box.sh` bottom

---

## Session 88 walk arc → why we're stopping to simplify

Walks #1 → #12 built up the per-pixel-lithology surface-paint pipeline:
- **#1-5**: strata bands (Y_tilted + XZ_cols), per-litho palettes, surface_y painters
- **#6**: per-pixel lithology mask path, concavity pass, varnish pass, cap_edge_stroke
- **#7**: cap dial-back (cap_edge_stroke was the issue)
- **#8**: cluster speckle, rock_zone_cleanup
- **#9-9.4**: concavity gating, varnish v2 (pure-flow), wash shrink
- **#10-10.2**: bilinear mask upscale, vein rock_gap re-gate, varnish escapes rock_gap, layer reorder, scipy.zoom speedup
- **#11**: joint_pattern + insolation_index + concavity_field masks, cliff_cap params bump, aspect-modulated varnish, low-res simplex (1:16 → upscale)
- **#12** (installed, walked): per-litho palette fixes, vein/varnish freq dial-back, cap tree-kill, concavity reordered (after talus, before varnish)

**Walk #12 in-world feedback:** "Veins now appear as dappled, vaguely network".

**Reflective decision at end of walk #12:** the system is overbaked. Eleven painting passes on rock_gap pixels, each with its own per-litho palette key. 11 × 6 = 66 palette config slots, 11 ordering decisions. The actual visual goals are 5. Walk #13 is a CONSOLIDATION walk, not a new-feature walk.

---

## Current state: 11 paint passes (overbaked)

Current pipeline on rock_gap pixels (walk #12 final):

```
biome surface → rock_gap → strata (rock_gap-only) → joint_pattern
→ veins (cross-cutting, rock_gap-only) → wash → bedrock_drainage
→ talus → CONCAVITY → varnish (drip-only, sharp slopes)
→ cliff_cap (suppresses trees + ground_cover) → rock_zone_cleanup → snow
```

Per-litho palette keys per group (11): `rock_gap_palette`, `strata.band_a`, `strata.band_b`, `speckle`, `wash_palette`, `bedrock_drainage_palette`, `talus_palette`, `vein_blocks`, `concavity_palette`, `varnish_palette`, `cap_palette`.

---

## Walk #13 plan: 11 paint passes → 7

### Visual goals (5)

1. **Host rock identity** — what kind of cliff is this
2. **Horizontal layering** — geological time-depth
3. **Cross-cutting accents** — faults / veins
4. **Cliff-base + flow deposits** — where stuff falls / where water moves
5. **Peak signature** — what crowns the cliff

### Consolidations

| Current pass | Walk #13 action | Rationale |
|---|---|---|
| **rock_gap** | KEEP | Goal #1, broadest coverage |
| **strata bands (band_a/b)** | KEEP | Goal #2, primary S88 feature |
| **speckle** | FOLD into strata description (no separate code path) | Already a sub-effect of band primary mix |
| **joint_pattern** | **DROP code path** | Real fix = improve XZ_cols (col_size + hash mode), not a second layer. Subtle/invisible on basaltics per walk #12 review. |
| **veins** | KEEP + IMPROVE | Goal #3. Streak length 5-14 → 12-30 blocks. Per-step ±1 perpendicular jitter (~30%). Threshold 192 → 160. Add cobblestone to all vein palettes (universal fault breccia). |
| **wash** | KEEP, ABSORB bedrock_drainage | Goal #4 |
| **bedrock_drainage** | **MERGE into wash** | "Small vs large drainage" is overprecision. Single wash pass with width fade. |
| **talus** | KEEP | Goal #4 |
| **concavity** | **DROP code path** | Walk #9.4 → walk #12 still couldn't get it to visibly read. Gated to lap≥5°+slope≥32°+dilate=2 = rarely fires; when it does it's lost under other layers. |
| **varnish** | KEEP | Goal #4 (drip-zone darkening — distinct from talus/wash) |
| **cliff_cap** | KEEP | Goal #5, kills trees + crowns peaks |
| **rock_zone_cleanup** | KEEP | Utility, required for snow-edge integrity |

### Simplified pipeline target

```
biome surface → rock_gap → strata (bands + speckle as one) → veins
→ wash (consolidated drainage) → talus → varnish → cliff_cap
→ cleanup → snow
```

**7 paint passes instead of 11.** Per-litho palette keys: 11 → 7 per group (36% reduction).

### Per-litho palette keys after walk #13

Each group keeps 7 palette config keys:
- `rock_gap_palette` (host rock identity)
- `strata.band_a` + `strata.band_b` (with cluster speckle integrated)
- `vein_blocks` (cross-cutting accents)
- `wash_palette` (absorbs bedrock_drainage)
- `talus_palette`
- `varnish_palette`
- `cap_palette`

**Drops:** `concavity_palette`, `bedrock_drainage_palette`. (`joint_pattern` never had a separate config key — just code that painted darker band primary — kill the code.)

---

## What stays from walks #1-#12

**Kept:**
- All strata machinery (Y_tilted + XZ_cols, per-litho compositions, cross-cutting orientation, halo zones)
- `masks/vein_field.tif` (walk #11)
- `masks/cliff_cap.tif` + tree-kill (walks #11/#12)
- `masks/varnish_field.tif` (walk #11)
- `masks/rock_gap.tif` 45° slope floor + fade band
- Cluster speckle inside strata bands
- `masks/concavity_field.tif` — baked but unused (keep on disk; cheap to revive)
- `masks/joint_pattern.tif` — baked but unused (keep on disk)
- `masks/insolation_index.tif` — baked but unused (future use: snow longevity + vegetation modulation)

**Dropped from code:**
- `_apply_concavity_drainage` function in `core/surface_decorator.py`
- `_apply_basaltic_joints` function (walk #11 addition)
- `_apply_bedrock_drainage` function — fold into `_apply_washes`
- Per-litho `concavity_palette` + `bedrock_drainage_palette` config keys
- Pipeline pass entries for these in `decorate_surface`

Estimated diff: ~200-300 lines deleted, no new code added.

---

## Walk #13 task plan (in order)

### #13.1 — Drop deprecated passes
1. Delete `_apply_concavity_drainage` + `_apply_basaltic_joints` + `_apply_bedrock_drainage` functions in `core/surface_decorator.py`.
2. Remove their pipeline call sites in `decorate_surface`.
3. Remove `concavity_field_tile` + `joint_pattern_tile` decorator kwargs (still kept in MASK_NAMES / file-on-disk for future).
4. Strip `concavity_palette` + `bedrock_drainage_palette` keys from `config/thresholds.json:lithology.groups.*` (script `tools/apply_s88_walk13_drop.py`).
5. Run `tools/validate_3x3.py --tile-x 89 --tile-z 52` to confirm no decorator crashes from missing config.

### #13.2 — Merge bedrock_drainage logic into wash
- Wash currently fires on `flow_acc > min_flow` + dilation + 5-block fade.
- Bedrock_drainage was firing on smaller flow + on rock_gap pixels.
- New combined wash: variable WIDTH based on flow_acc magnitude (3 blocks wide at min_flow, up to 8 blocks wide at high flow). Palette stays per-litho `wash_palette` (no merge of bedrock_drainage_palette into it — wash_palette absorbs the role).
- Test on (24,80) arid_basaltic — should see continuous wash corridors at both small and large flow scales.

### #13.3 — Vein streaking fix (the walk #12 dappled feedback)
File: `core/surface_decorator.py:_apply_strata_veins_surface`
1. **Streak length** 5-14 → 12-30 blocks.
2. **Per-step ±1 perpendicular meander** (~30% chance per step) inside the streak extension loop.
3. **Threshold rebalance**: `vein_mask_threshold` 192 → 160 (slightly more seeds).
4. **Amp stays** at 0.18.
5. **Halo zone (varnish_palette ring)** STAYS — alteration aureole.

### #13.4 — Per-litho palette reorder (with cobblestone added to all veins)

Apply via new `tools/apply_s88_walk13_palette.py` script.

**granitic** (felsic igneous, warm tones)
- rock_gap: granite + andesite
- band_a: granite 80% / dripstone_block 20% (hydrothermal quartz facies)
- band_b: andesite 80% / granite 20% (mafic facies)
- veins: **cobblestone** + iron_block + raw_iron_block + dripstone_block
- wash: coarse_dirt + gravel + dripstone_block (absorbing bedrock_drainage role)
- talus: cobblestone + coarse_dirt + gravel
- varnish: andesite + stone
- cap: granite + dripstone_block

**arid_basaltic** (mafic igneous, dark + warm dust)
- rock_gap: basalt + smooth_basalt
- band_a: basalt 85% / gray_concrete_powder 15%
- band_b: smooth_basalt 85% / blackstone 15%
- veins: **cobblestone** + iron_block + dripstone_block + soul_soil
- wash: gray_concrete_powder + cobbled_deepslate + mud
- talus: packed_mud + coarse_dirt + basalt
- varnish: mud + blackstone (classic manganese desert varnish)
- cap: blackstone + smooth_basalt

**temperate_basaltic** (mafic, weathered)
- rock_gap: deepslate + cobbled_deepslate + tuff
- band_a: cobbled_deepslate 85% / deepslate 15%
- band_b: deepslate 80% / tuff 20%
- veins: **cobblestone** + tuff + black_concrete_powder
- wash: gray_concrete_powder + tuff + mud
- talus: cobbled_deepslate + tuff + coarse_dirt
- varnish: tuff + mud
- cap: blackstone + black_concrete_powder

**limestone / karst** (sedimentary carbonate, light)
- rock_gap: calcite + diorite + dripstone_block
- band_a: calcite 80% / diorite 20%
- band_b: diorite 80% / dripstone_block 20%
- veins: **cobblestone** + dead_horn_coral_block + dead_fire_coral_block + calcite
- wash: white_concrete_powder + light_gray_concrete_powder + clay
- talus: calcite + white_concrete + gravel
- varnish: tuff + andesite + stone (dark on white — high contrast)
- cap: dead_horn_coral_block + calcite + andesite (fossil-rich karst)

**deepslate_metamorphic** (medium gray)
- rock_gap: andesite + stone
- band_a: andesite 90% / stone 10%
- band_b: stone 90% / andesite 10%
- veins: **cobblestone** + basalt + smooth_basalt
- wash: tuff + pale_moss_block + mud
- talus: packed_mud + coarse_dirt + suspicious_gravel
- varnish: basalt + smooth_basalt
- cap: basalt + smooth_basalt

**mossy_temperate** (any rock + cool wet biota)
- rock_gap: cobblestone + mossy_cobblestone
- band_a: cobblestone 60% / mossy_cobblestone 40%
- band_b: mossy_cobblestone 60% / cobblestone 40%
- veins: **cobblestone** + moss_block + pale_moss_block
- wash: moss_block + mud + packed_mud (NO soul_soil)
- talus: mossy_cobblestone + cobblestone + moss_block
- varnish: moss_block
- cap: mossy_cobblestone + tuff + pale_moss_block

### #13.5 — Render + walk
1. Push commits, dispatch 6-box on same 6 pure-litho tile set.
2. Install MCAs to Vandirtest10.
3. User walks → feedback for walk #14 (if needed).

---

## Walk #14+ deferred backlog (DO NOT include in walk #13)

These were queued in walk #12 backlog (`memory/S88_walk12_handoff.md`); now superseded by walk #13 simplification or moved later:

- **Granitic band_a vs band_b differentiation** — captured in walk #13.4 palette reorder (band_a = granite/dripstone, band_b = andesite/granite).
- **Concavity mask consumption** — DROPPED (concavity pass removed entirely).
- **Insolation mask consumption** — DEFERRED to walk #14+. (Snow longevity + per-biome warm/cold density modulation.)
- **Avalanche scars** — DEFERRED. Build precompute mask + paint pass.
- **amp_boost NE-clamp + north_factor only-up rescale** — DEFERRED.
- **`_apply_desert_rock_palette` dead code purge** — DEFERRED, walk #13 already deletes 3 other functions; do this in a separate cleanup walk.
- **Cluster speckle ported to chunk_writer basement** — DEFERRED.
- **Joint pattern visibility check** — N/A (joint pattern paint pass dropped entirely in walk #13).

---

## Critical session-spanning context (carry forward)

- **Mask build pattern**: each cloud render dispatches → builds derivative masks on each box (~3-5 min with scipy zoom + low-res simplex 1:16) → renders pipeline → collects MCAs → installs to Vandirtest10. See `cloud_bake/render_s88_litho_4box.sh` and dispatch scripts in `/tmp/walk*_dispatch.sh`.
- **`opensimplex.noise2array` performance gotcha**: pure-Python, 156M cells = 10-15 min single-threaded. Walk #11 v4 fix: compute at 1:16 (10M cells, ~30s) then `scipy.ndimage.zoom(order=1)` upscale to 1:4.
- **`scipy.ndimage.zoom` is the right tool** for any mask upscale at 50k scale — BLAS-backed, multi-threaded. Don't use PIL or hand-rolled np.ix_ vectorization (both 10-50x slower).
- **Bilinear upscale preserves smooth simplex/laplacian fields** without the 4-block stairstep stamps NEAREST creates at signal boundaries. Walk #10.1 fix.
- **Stale-snapshot trap**: dispatch script's monitor was fooled by leftover `/root/render_done` on respun Hetzner boxes. Fix in `/tmp/walk10_dispatch.sh`: explicit `rm /root/render_done` in the pre-pull cleanup phase.
- **User strong opinions:**
  - DO NOT change palettes without permission — assess CURRENT palettes by color, use that to inform new palette choices (walk #8 lesson).
  - Add cobblestone to all vein palettes (universal fault breccia).
  - Wash stays inside rock_gap gate (walk #10 user revert).
  - Veins should read as STREAKS, not DAPPLES.

---

## Active config knobs (walk #12 → carrying into walk #13)

```
rock_gap: slope_solid 45°, fade 1 block, morph_close 2
strata fade-in: 32-35° slope
vein_mask_threshold: 192 → walk #13 target 160
vein streak: 5-14 → walk #13 target 12-30 blocks, ±1 perpendicular meander 30%
varnish: amp 0.25, dilate 1, mask slope 60-80°, north-modulated, escapes rock_gap
cliff_cap: threshold 8, dilate 12, suppress_trees + kill_ground_cover
washes: min_flow 0.001, dilation 2, fade 5 → walk #13 target: variable width by flow_acc
trees: slope_penalty 55-75°
basaltic XZ_cols: col_size 16, hash mode "diagonal" (aspect-perpendicular)
```

---

## Files to read before starting walk #13

1. This document.
2. `memory/S88_walk12_handoff.md` (prior walk's plan, contains palette reorder ORIGINAL proposal that walk #13 simplifies further).
3. `core/surface_decorator.py` — focus on `_apply_strata_veins_surface`, `_apply_concavity_drainage`, `_apply_basaltic_joints`, `_apply_bedrock_drainage`, `_apply_washes`, `_apply_rock_varnish`, `_apply_cliff_cap` painters.
4. `config/thresholds.json:lithology.groups` — all 6 group configs.
5. `tools/apply_s88_walk12.py` — pattern for the config-patch scripts walk #13 needs.
6. `run_pipeline.py` — verify mask plumbing into `decorate_surface` + `place_schematics`.
