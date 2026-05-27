# S88 Walk #12 → Walk #13 Handoff

**Branch tip:** `e53587d` on `s85-cherry-picks`
**Date:** 2026-05-27 (session 88 ongoing)
**Test world:** Vandirtest10
**Active 6-tile pure-litho set:** (72,60) granitic / (24,80) arid_basaltic / (89,52) temperate_basaltic / (36,15) limestone / (19,44) deepslate / (64,72) mossy_temperate
**Coords:** see `cloud_bake/render_s88_litho_4box.sh` bottom

## Session 88 walk arc (walks #1 → #12)

S88 set out to add geological strata realism. Each walk addresses ONE in-world complaint round:
- **#1-5**: strata bands (Y_tilted + XZ_cols), per-litho palettes, surface_y painters
- **#6**: per-pixel lithology mask path, concavity pass, varnish pass, cap_edge_stroke
- **#7**: cap dial-back (cap_edge_stroke was the issue)
- **#8**: cluster speckle, rock_zone_cleanup
- **#9-9.4**: concavity gating, varnish v2 (pure-flow), wash shrink
- **#10-10.2**: bilinear mask upscale, vein rock_gap re-gate, varnish escapes rock_gap, layer reorder, scipy.zoom speedup
- **#11**: joint_pattern + insolation_index + concavity_field masks, cliff_cap params bump, aspect-modulated varnish, low-res simplex (1:16 → upscale)
- **#12** (latest, awaiting in-world review): per-litho palette fixes, vein/varnish freq dial-back, cap tree-kill, concavity reordered (after talus, before varnish)

## Walk #12 in-flight changes (dispatched `b6is8sfmd`, awaiting render)

### Per-litho palettes
- **arid_basaltic**: talus = packed_mud+coarse_dirt; veins = iron_block+dripstone_block+soul_soil; concavity = tuff+pale_moss_block; varnish = mud+blackstone
- **granitic**: band_a = band_b (granite 80%/rooted_dirt 20% — single composition, no alternation); concavity = soul_soil+soul_sand+coarse_dirt; bedrock_drainage = coarse_dirt+dripstone; varnish = andesite+stone

### Global
- `strata.vein_mask_threshold` 96 → 192 (way less common)
- `per-litho vein_amp` 0.4 → 0.18
- `varnish.amp` 0.5 → 0.25; `dilate_blocks` 6 → 1
- Mask builder: `VARN_SLOPE_MIN_DEG` 32→60, `VARN_SLOPE_MAX_DEG` 60→80 (drip-only)
- `cliff_cap.suppress_trees` + `kill_ground_cover` = True

### Code changes
- `schematic_placement.place_schematics` accepts new `cliff_cap_tile` kwarg → suppresses tree placement on cap pixels
- `surface_decorator` cap painter zeros `ground_cover` on cap pixels when `kill_ground_cover` enabled
- Pipeline reorder: concavity moved from before-veins to **after-talus, before-varnish**

## Current pipeline order (walk #12 final)
```
biome surface → rock_gap → strata (rock_gap-only) → joint_pattern
→ veins (cross-cutting, rock_gap-only) → wash → bedrock_drainage
→ talus → CONCAVITY → varnish (drip-only, sharp slopes)
→ cliff_cap (suppresses trees + ground_cover) → rock_zone_cleanup → snow
```

## Mask file inventory (all built by `tools/build_vein_and_cap_masks.py`)

| Mask | Walk added | Size | Detection |
|---|---|---|---|
| rock_gap.tif | pre-S88 | 50k uint8 | Gaea slope >= 32° + morph_close iter 2 |
| cliff_cap.tif | walk #10 (rebuilt walk #11) | 88MB | edge caps + convex peaks + gaussian fade band; search 40, fade 10 |
| varnish_field.tif | walk #11 | 549MB | slope-faded drip-flow band × (0.5 + north_factor) |
| vein_field.tif | walk #10 | 1.1GB | laplacian + simplex fault network (mainline + branches), slope >= 32° |
| joint_pattern.tif | walk #11 | uint8 | simplex-perturbed grid boundaries, col_size 12 blocks |
| insolation_index.tif | walk #11 | uint8 | south_factor * slope_norm (baked, unused yet) |
| concavity_field.tif | walk #11 | uint8 | laplacian >= 3.0 + slope >= 32° + dilate 2 (baked, unused yet — runtime still computes) |

## Per-litho palette state at walk #12 dispatch

(See `tools/apply_s88_walk12.py` for the diff. Walk #11 dump in chat.)

## Active config knobs (walk #12)

```
rock_gap: slope_solid 32°, fade 1 block, morph_close 2
strata fade-in: 32-35° slope
vein_mask_threshold: 192 (top ~1-2% of mask survive)
vein streak: 5-14 blocks, cross-cutting perpendicular to strata axis, halo=varnish_palette
varnish: amp 0.25, dilate 1, mask slope 60-80°, north-modulated, escapes rock_gap
concavity: lap_thr 5.0, slope_min 32°, dilate 2, palette per group (own key)
cliff_cap: threshold 8, dilate 12, suppress_trees + kill_ground_cover
washes: min_flow 0.001, dilation 2, fade 5
trees: slope_penalty 55-75° (was 35-50)
joints (basaltics): 12-block columns w/ simplex jitter
basaltic XZ_cols: col_size 16, hash mode "diagonal" (aspect-perpendicular)
```

## Open items / walk #13 candidates

### From walk #12 anticipated feedback
- **Granitic single-band**: band_a = band_b now. User may want SLIGHT differentiation back. If so: band_a = granite 90%/dripstone 10%, band_b = granite 80%/rooted_dirt 20% (subtle hint at variation while keeping granite-dominant).
- **Veins still "blobby" not windy**: walk #12 reduces count but doesn't add per-streak meandering. If user reports it: implement directional jitter inside `_apply_strata_veins_surface` streak extension (each step has ±1 perpendicular wobble chance).
- **Mask reorder for realism (not yet implemented)**: User asked for proposal. See "Palette reorder proposal" section below.
- **Cliff_cap may still need to extend** to cover BOTH cliff-top-edges AND convex peaks more aggressively. Current walk #11 build has both signals; walk #12 doesn't touch builder params. If user wants more: bump CAP_SEARCH_BLOCKS 40→60 or CAP_PEAK_LAP_THR 1.0→0.5 (catches gentler peaks).
- **Tree placement on cap pixels**: walk #12 wires it. Verify in-world that trees actually skip cap zones.

### Palette reorder proposal (NOT IMPLEMENTED — user requested as plan)

Current per-litho palettes feel mismatched at the "what block represents what feature" level. Proposed reorganization for geological coherence:

**granitic** (felsic igneous, warm tones)
- rock_gap: granite + andesite (warm intrusive igneous)
- band_a: granite 80% / dripstone_block 20% (granitic with hydrothermal quartz veining)
- band_b: andesite 80% / granite 20% (slightly more mafic facies)
- speckle: raw_iron_block (iron-rich mineralization)
- wash: coarse_dirt + gravel (mountain stream gravels)
- bedrock_drainage: coarse_dirt + dripstone_block + raw_iron_block (iron-stained drainage)
- talus: cobblestone + coarse_dirt + gravel (typical granite scree)
- veins: iron_block + raw_iron_block + dripstone_block (iron + hydrothermal silica)
- concavity: coarse_dirt + dripstone_block (sediment in depressions)
- varnish: andesite + stone (oxidized darkening on granite)
- cap: granite + dripstone_block (resistant granite outcrop top)

**arid_basaltic** (mafic igneous, dark + warm dust)
- rock_gap: basalt + smooth_basalt
- band_a: basalt 85% / gray_concrete_powder 15%
- band_b: smooth_basalt 85% / blackstone 15%
- speckle: mud / packed_mud (volcanic ash fall)
- wash: gray_concrete_powder + cobbled_deepslate (dark wash)
- bedrock_drainage: blackstone + mud + tuff (manganese-rich drainage)
- talus: packed_mud + coarse_dirt + basalt (basalt scree + ash)
- veins: dripstone_block + tuff (cooling-joint silica)
- concavity: mud + packed_mud (ash accumulation in depressions)
- varnish: mud + blackstone (classic desert varnish: dark manganese coating)
- cap: blackstone + smooth_basalt (resistant flow top)

**temperate_basaltic** (mafic, weathered)
- rock_gap: deepslate + cobbled_deepslate + tuff (more weathered surface)
- band_a: cobbled_deepslate 85% / deepslate 15%
- band_b: deepslate 80% / tuff 20%
- speckle: dripstone_block + packed_mud
- wash: gray_concrete_powder + tuff (paler wet weathering)
- bedrock_drainage: tuff + mud + andesite (weathered drainage)
- talus: cobbled_deepslate + tuff + coarse_dirt (mossy scree)
- veins: tuff + black_concrete_powder (chloritized fractures)
- concavity: mud + tuff (sediment accumulation)
- varnish: tuff + mud (subtler dark in wetter climate)
- cap: blackstone + black_concrete_powder

**limestone / karst** (sedimentary carbonate, light)
- rock_gap: calcite + diorite + dripstone_block
- band_a: calcite 80% / diorite 20%
- band_b: diorite 80% / dripstone_block 20% (thinner bedded chalk)
- speckle: andesite (chert nodules in chalk)
- wash: white_concrete_powder + light_gray_concrete_powder (carbonate-saturated wash)
- bedrock_drainage: clay + calcite (karst sink drainage)
- talus: calcite + white_concrete + gravel (carbonate scree)
- veins: dead_horn_coral_block + dead_fire_coral_block + calcite (fossil + crystalline calcite)
- concavity: clay + dripstone_block (sink-hole infill)
- varnish: tuff + andesite + stone (dark manganese on white — high contrast)
- cap: dead_horn_coral_block + calcite + andesite (fossil-rich karst cap)

**deepslate_metamorphic** (high-grade metamorphic, dark gray)
- rock_gap: andesite + stone (medium gray)
- band_a: andesite 90% / stone 10%
- band_b: stone 90% / andesite 10%
- speckle: tuff + deepslate (mineral inclusions)
- wash: tuff + pale_moss_block (subtle wet)
- bedrock_drainage: tuff + mud + coarse_dirt
- talus: packed_mud + coarse_dirt + suspicious_gravel
- veins: basalt + smooth_basalt (mafic dikes intruding metamorphic)
- concavity: mud + coarse_dirt
- varnish: basalt + smooth_basalt (subtle further darkening of dark host)
- cap: basalt + smooth_basalt (resistant mafic cap)

**mossy_temperate** (any rock + cool wet climate biota)
- rock_gap: cobblestone + mossy_cobblestone
- band_a: cobblestone 60% / mossy_cobblestone 40%
- band_b: mossy_cobblestone 60% / cobblestone 40%
- speckle: moss_block + pale_moss_block + andesite
- wash: moss_block + mud (wet moss bed)
- bedrock_drainage: mud + moss_block + packed_mud (NO soul_soil)
- talus: mossy_cobblestone + cobblestone + moss_block (mossy scree)
- veins: moss_block + pale_moss_block (organic infill)
- concavity: mud + moss_block + packed_mud (organic mat accumulation)
- varnish: moss_block (water = moss in temperate)
- cap: mossy_cobblestone + tuff + pale_moss_block

### Other walk #13 backlog
- **Avalanche scars** (planned walk #11+ originally, deferred): build precompute mask + paint pass
- **amp_boost NE-clamp** in eco_gradients (SW modifier inappropriately reduces NE rock_gap)
- **north_factor only-up rescale** (`0.5 + 0.5×nf` so south baseline isn't 0)
- **`_apply_desert_rock_palette` dead code purge** (~200 lines unused)
- **Cluster speckle ported to chunk_writer basement** so y-2 character matches surface speckle
- **Concavity mask consumption** (replace runtime laplacian with `concavity_field.tif` read)
- **Insolation mask consumption** (vegetation density modulator, snow longevity)
- **Veins windy meander**: per-step ±1 perpendicular jitter in streak extension (real geological veins meander, not perfectly straight)
- **Joint pattern visibility check**: walk #11 added basalt joints; verify they're visible on basaltic cliffs in walk #11/#12 renders. If not, may need to scale up col_size or paint with higher contrast block (e.g. blackstone instead of basalt for arid).

## Critical session-spanning context

- **Mask build pattern**: each cloud render dispatches → builds 6 derivative masks on each box (~3-5 min with scipy zoom + low-res simplex 1:16) → renders pipeline → collects MCAs → installs to Vandirtest10. See `cloud_bake/render_s88_litho_4box.sh` and dispatch scripts in `/tmp/walk*_dispatch.sh`.
- **`opensimplex.noise2array` performance gotcha**: pure-Python, 156M cells = 10-15 min single-threaded. Walk #11 v4 fix: compute at 1:16 (10M cells, ~30s) then `scipy.ndimage.zoom(order=1)` upscale to 1:4.
- **`scipy.ndimage.zoom` is the right tool** for any mask upscale at 50k scale — BLAS-backed, multi-threaded. Don't use PIL or hand-rolled np.ix_ vectorization (both 10-50x slower).
- **Bilinear upscale preserves smooth simplex/laplacian fields** without the 4-block stairstep stamps NEAREST creates at signal boundaries. Walk #10.1 fix.
- **Stale-snapshot trap**: dispatch script's monitor was fooled by leftover `/root/render_done` on respun Hetzner boxes. Fix in `/tmp/walk10_dispatch.sh`: explicit `rm /root/render_done` in the pre-pull cleanup phase.
