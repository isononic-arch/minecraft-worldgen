# S89 Handoff — Rock + Snow + Vegetation Surface System

**Date:** 2026-06-03 | **Branch:** `s85-cherry-picks` (merged to master) | **Flags:** `lithology.rock_layers.enabled` + `snow_physics.enabled` committed **TRUE**.

This is the running handoff for the S89 megasession: the full physically-driven rock + snow + vegetation surface overhaul, validated on 6 lithology/alpine tiles and now flag-flipped on. **Next major step is a full-biome validation sweep, then the 50k world regen.**

---

## 1. What shipped (by layer)

### Lithology / rock (`core/surface_decorator.py`, `tools/build_terrain_derived.py`)
- **rock_layers** slope tiers dark/mid/light @ **40/45/50°**, per-lithology-group palettes (6 groups), painted via `_apply_rock_layers` with 50/50 dither. Mask: `rock_layers.tif`.
- **Foliation ribs** (`washes.foliated`): thin **real flow lines** filtered to the cross-cutting tributaries (`|cos(∇flow,∇height)| > cross_cos`), painted MID tier with a **contrast-swap** (DARK where the rib crosses native mid-tier so it always reads). NOT procedural.
- **Washes** (`washes`): flow-proportional width (thin summit → wide base), **edge-only salt-pepper speckle** (`edge_fade_blocks`), slight horizontal **dither** (`dither_blocks`). flow_tile is NORMALIZED [0,1].
- **Talus** aprons, **cliff_cap** convex-peak scour, **rock varnish**, **concavity drainage** — all per-group palettes.
- **Relief** (`lithology.rock_layers.relief`): un-smooths rock terrain. **Base pass** (slope_gain×smooth_gain on rock tiers) + **subtle snow-zone pass** (`relief.peak`: amp 1.2, fires across the snow zone, **fades to 0 at the snowline** via `snowline_fade_blocks`). The snow relief is **additive ON TOP of** the base relief (two `surface_y +=`, independent seeds). MUST run after the boundary-Y smoothers.
- Subsurface strata = LIGHT tier (granitic=DARK); powders→hardened concrete. temp_basaltic dark = cobbled_deepslate/smooth_basalt.

### Snow (`core/surface_decorator.py`, `core/eco_gradients.py`, `tools/build_snow_physics.py`)
The snowline is now a **multi-factor field + patchy band**, not an altitude cutoff:
- **Per-biome base line** (`snow_lines`): snowy (SBT/ARCTIC/FROZEN) ~400, BOREAL_ALPINE 540, BOREAL_TAIGA 600, temperate 625-650, dry/warm 655-760. Climate-ordered (cold low, warm high); the local wobble (±~60) never crosses biomes.
- **Aspect** (`snow_lines.aspect_coeff=70`): `line += coeff·(0.5 − north_factor)` — south/sunny raises, north/shaded lowers (±35).
- **Convexity** (`convexity_coeff=3.5`): ridges (convex) higher, gullies (concave) lower.
- **Patchy transition band** (`transition_blocks=25` + `micro_*_coeff`): replaces the hard `surface_y >= line`. Survival = `t(altitude) + micro_curv·fineConcavity + micro_aspect·shade + micro_potential·snow_potential`. Snow lingers in hollows/shade/**physics drifts & couloirs** (`snow_potential` = Winstral Sx + curvature = wind + hydrology for free), melts on bumps/sun/scoured. ~50-block patchwork edge.
- **Sources:** `gap_source="gaea"` → Gaea `snow_gap` is the base (gap==7 → snow_block, altitude-gated by SNOW_Y_FLOOR 395 / CEIL 475). **depth-snow** `gully_only` adds snow_block fingers down concave gullies anchored on the (aspect/convexity-adjusted) per-biome line, `gully_drop_blocks=45` below. `snow_carpet` (dappled layers=1) for SBT/FF. `snow_potential.tif` (continuous, built by build_snow_physics with `base_floor=0.0`) feeds the gully + the patchy band's drift term.
- **Runtime:** `frozen_peaks` biome (cold, was `stony_peaks` temp 1.0 which MELTED our snow) emitted on snowy-biome rock cells (`chunk_writer`) — MC 4×4 biome granularity so it's dilated. Server weather behavior UNTESTED.

### Vegetation / schematics (`core/schematic_placement.py`, `core/chunk_writer.py`)
- **Tree SINK** (chunk_writer): trees on slopes SINK so the trunk meets ground and the canopy drops (no floaters, no flagpoles). `MAX_TRUNK_EXT=8` (residual fill) + `MAX_TREE_SINK=16` (reject beyond). Mirrors the bush-sink. The `_post_decorate_y` lock in run_pipeline restores land surface_y before write so columns match anchors.
- **Krummholz** (`krummholz`): real-**height**-gated tiny pines (size codes are MISLABELED — measures geometry via `_krummholz_tree_height`, ≤`max_height_blocks=7` → `ppine_g_lg`(6) + `scotsp_a_sm`(5)). Fires near rock (gap==5 +`rock_dilate_blocks`), near snow caps (`snowcap_dilate_blocks=30`), and altitude-feathered (550-600).
- **Ecotone conifer filter**: a swapped tree cell whose ORIGINAL biome is conifer/snowy keeps ONLY conifer species (no birch/oak in snow). Conifer-conifer mixing preserved.
- **Per-biome canopy radius** (`tree_spacing`): the exclusion radius is the true density ceiling; conifers pack tighter (radius_mult 0.6-0.8), clamped to a log-safe floor (trunks never share a column). **Ecotone max-blend** (`ecotone_density_blend="max"`) holds the dense side's density into the seam.
- **Global reject**: `dpine_tree_scotsp_b_sm` killed (stripped_dark_oak trunk read as broadleaf). Audit for other mislabeled schematics.
- **Grass terraces** (`grass_terraces`): grass on flat rock benches (locally flat + surrounded by steep), edge salt-pepper fade, snow-handoff, excludes arid biomes.

---

## 2. Config map (where to tune)
| Want to change | Knob |
|---|---|
| Snow line per biome | `snow_lines.<BIOME>` (+ `_default`) |
| Aspect strength | `snow_lines.aspect_coeff` |
| Ridge/gully snow shift | `snow_lines.convexity_coeff` |
| Patchy band width / character | `snow_lines.transition_blocks`, `micro_{curv,aspect,potential}_coeff` |
| Gully finger depth | `snow_physics.depth.gully_drop_blocks` |
| Snow-cap solidity threshold | `snow_physics.depth.t_block` |
| Rock tier slopes | `lithology.rock_layers.t1/t2/t3` (40/45/50) |
| Rib density / cross-cutting | `washes.foliated.cross_cos`, `min_flow` |
| Wash width | `washes.width_min/max`, `edge_fade_blocks`, `dither_blocks` |
| Relief crag | `lithology.rock_layers.relief.amp_blocks` (base) + `.peak.amp_blocks` (snow zone) |
| Krummholz size cap / triggers | `krummholz.max_height_blocks`, `rock_dilate_blocks`, `snowcap_dilate_blocks`, `feather_lo/hi_y` |
| Forest density per biome | `tree_spacing.radius_mult_by_biome` |
| Grass terrace amount | `grass_terraces.coverage / surround_min_deg / bench_max_deg / rock_dilate_blocks` |

---

## 3. Hard-won gotchas (do not relearn)
1. **`snow_potential.tif` size**: with `base_floor=0.3` it was 3.4 GB (incompressible). Now `0.0` → should be small. RE-CHECK after rebuild.
2. **flow_tile is NORMALIZED [0,1]** (tile_streamer /65535). All wash/foliated/flow thresholds must be normalized, NOT raw.
3. **Relief mutates surface_y** → `_post_decorate_y` lock in run_pipeline restores land cells before write_tile so trees don't float. Any future re-smooth of surface_y after Step 8 brings floaters back (antipattern #2).
4. **Schematic size codes (sm/md/lg) LIE** — `ppine_g_lg` is 6 tall, `ppine_d_md` is 42. Measure geometry. Audit other height-dependent code.
5. **SBT/FF snow ≠ other biomes' snow**: SBT/FF use carpet+gully+runtime (biome-wide), NOT the per-biome line gate (they're exempt from the Gaea snow_block consumer). The aspect/patchy realism shows mainly on BA/temperate/dry.
6. **Stale client chunks**: copying region files under a loaded world does NOT re-read — FULLY QUIT Minecraft.
7. **Ecotone filter only catches SWAPPED cells** — a BIRCH_FOREST override pixel at snow altitude places birch directly (not a swap). 2 stray birch survived on the pure-SBT tile.

---

## 4. Render workflow
- **6-box CCX63 cloud**: `bash cloud_bake/render_s89_rocksnow.sh <6 IPs>` — git reset --hard origin/$BRANCH, builds masks on-box (`build_terrain_derived.py --only rock_layers,talus,cap --scale 8` + `build_snow_physics.py --scale 8`), renders 6 litho tiles, installs to Vandirtest10. ~16 min, ~$1-2.
- Tiles: (72,60)granitic=SBT, (24,80)arid_basaltic, (89,52)temperate_basaltic=conifer, (36,15)limestone=KARST/STEPPE, (19,44)deepslate_metamorphic, (64,72)mossy_temperate.
- Now that flags are committed ON, a plain `run_pipeline.py` also produces the S89 system (the script's box-flip is now a no-op).
- **Recopy**: `cp output_s89_rocksnow/r.*.mca <Vandirtest10>/region/`, md5-verify, FULLY QUIT MC.

---

## 5. NEXT STEPS (priority order)
1. **Validate `snow_potential.tif` size** — ✅ DONE. 76 MB (sparse uint8), committed `5228070`. Gate cleared.
2. **Full-biome validation sweep** — the 6 litho tiles cover 6 groups; ~20 of 26 biomes were unvalidated under the full S89 stack. **Render script ready: `bash cloud_bake/render_s89_sweep.sh IP1..IP8`** (32 tiles = 26 biome + 6 rock, round-robins 4/box across 8 CCX63, ~30-35 min wall, builds the 4 scale-8 masks on-box, installs to Vandirtest10, prints precise land TP commands). Walkable list + findings in `memory/S89_validation_sweep.md`; checklist in `memory/BIOME_VALIDATOR_CHECKLIST.md`.
   - **Snow findings (2026-06-03, decided LEAVE AS-IS):** snow is terrain-limited, not line-limited. Only ARCTIC_TUNDRA (maxY 699) + SNOWY_BOREAL_TAIGA (582) reach their 400 lines, so they're the only biomes that snow; all the world's high peaks are painted ARCTIC_TUNDRA. Dry/warm "peaks only" snowlines (655-760) are unreachable → those biomes never snow (intended, non-event). BOREAL_ALPINE never snows because its terrain tops at Y171 (line 540 is moot) — a biome-placement quirk, not a line bug. User chose to leave all snow lines unchanged.
   - **✅ `memory/biome_reference_tiles.csv` REGENERATED land-aware (2026-06-03).** `tools/diag_biome_sampler.py` was rewritten: applies the live `config.terrain_spline` to `height.tif` (decimated 1:8 reads), gates every tile on surface MC-Y ≥ 63, picks the TP point from actual land pixels, and derives TP-Y from real terrain height + 40 headroom (commands always land you above ground, never in ocean). RIPARIAN_WOODLAND + FRESHWATER_FEN now present. CSV has TWO sections (`kind` column): **26 `biome` rows** (one clean land tile each) + **6 `rock` rows** (one *highest-altitude* exposed-rock tile per lithology group — high mountains: deepslate Y694, granitic Y677, limestone Y672, arid_basaltic Y652, temperate_basaltic Y643, mossy_temperate Y321). Checklist mirror at `memory/BIOME_VALIDATOR_CHECKLIST.md`.
     - **The rock section keys off the PAINTED `masks/lithology.tif` group-id raster (1:8, gids granitic=1/arid_basaltic=2/temperate_basaltic=3/limestone=4/deepslate=5/mossy=6), NOT biome→`zone_to_group`.** Inferring group from the dominant biome is WRONG — e.g. temperate_basaltic rock sits under high-altitude conifer/tundra biomes, so a biome-based scan only finds the low coastal tiles (gave a bogus Y163). Per S88 walk #4d, `lithology.tif` is the sole per-pixel group source; the sampler mirrors that.
     - **Tool gotcha baked in:** `rock_gap.tif` is `{0,1}` — read it with a FLOAT out-array + average resampling (`_read_dec_frac`), NOT uint8 average (which rounds the per-cell fraction to 0 → "no rock found").
     - Spline polarity confirmed: pipeline + tool both use `np.interp(raw, gaea_in, mc_y_out)`, HIGH raw = HIGH terrain (column_generator.py:811, "Session 13"). MEMORY.md's inverted-polarity note is STALE.
     - Coastal/transitional biomes (MANGROVE 30%, ETC 41%, RIPARIAN 49%, FEN 52% land) share their tile with ocean — that's the best pure-land tile available; TP still lands on real biome pixels.
3. **Server-side `frozen_peaks` weather test** — the world is Paper/Spigot-destined; verify snow accumulation/melt behavior.
4. **Floating-tree spot-check** on water-adjacent + steep tiles (the lock is fragile).
5. **Full 50k regen** — only after the sweep passes. 9409 tiles; estimate time/cost first.

See the full per-layer **callouts checklist** in the §18 log entry / the session transcript.
