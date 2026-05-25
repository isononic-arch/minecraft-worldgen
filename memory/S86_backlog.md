# S86 Backlog — 2026-05-24

User-supplied backlog (verbatim, after S85 cloud render review).

## Pre-action safety
- **Don't break anything on biome fix (BT-banding repaint).** Be ready to revert.
- **Generate fresh false-color AT/BT/SBT/tundra biome map** post-banding and compare
  against the most recent S85 reference (`cold_biomes_map.png`, `ba_bt_sbt_at_coverage.png`).

## Global issues

### 1. Water tick fix didn't fully work
S85 commit `e0794dd` (skip fluid ticks for above-sea river water) — rivers are
still updating in-world, just less. Need to re-investigate. Likely fluid ticks
are still being scheduled somewhere, or the above-sea check isn't catching all
river water columns.

### 2. Peak dirt scattering on AT
On the AT tile from S85 cloud render, dirt + mud is scattered across peaks.
User likes the **distribution** but suspects it's an artifact from a session
merge (S85 reconciliation). Need to identify the source — is this a leftover
from a pre-S70 surface-decoration pass that survived the merge?

### 3. Transition zone (ecotone) blobs too big + unsightly
S85 Option A ecotone shadow lookup is better than the harsh simplex bounds we
had earlier, but the blobs are still too big AND don't seem to match their
representative biomes' palettes. User suspects the lookup is selecting blocks
that don't actually belong to the swap-in biome.

### 4. Mountain peaks too soft
Peaks need actual TERRAIN variation, not just block-swap noise — think
VoxelSniper `/b e melt` or `/b e lift` before `/b e smooth`. Soft peaks look
fake. Need a terrain-shaping noise modifier on AT/SBT peak surfaces.

### 5. Washes are too subtle
Look good as-is, but should be:
- More intense (wider)
- Lower threshold (more representation)
- Plunge **3 blocks down** into the land (not just surface swap) — duplicate/
  randomize values a few blocks deep so they look like real washes when slope
  is significant. Currently a single block over each layer reveals underlying
  rock on slopes.

### 6. Lithology mask reads are off in multiple tiles
Code is reading a stale lithology override or mapping. Specific tiles where
in-game appearance doesn't match the documented zone_to_group lithology:
- **15,61** (granitic in mask → basaltic in game) — washes are wrong palette
- **38,15** (limestone/karst area, too much rock gap)
- **89,52** (deepslate_metamorphic in mask → basaltic in game) — mapping updated, code stale
- **10,77** (basaltic — no good read from this tile)
- **40,28** (limestone — looks good but too much rock gap representation)

→ Confirm `config/thresholds.json` lithology mapping is up-to-date AND the code
path actually reads it (not a hardcoded copy somewhere).

### 7. Rock gap mask too aggressive on low-slope areas
The Gaea slope-derived rock_gap (5) mask is appearing on shallow hills where
user expects just trees. User flagged tiles **28,7** and **38,15** specifically.
Possible causes:
- Spline change since S60 means rock-gap slope threshold needs re-tuning
- User previously said "let the gap mask go where it wants regardless of slope" —
  consider rolling back that decision and re-clamping with a stricter slope floor
- Slope norm interpretation drift after world-height scaling

→ Investigate `core/eco_gradients.py` rock-gap slope thresholds + per-tile Gaea
slope distributions.

### 8. Schematic clone-rotation issue
At **20,36** (birch forest), trees are placed next to their exact duplicate
without rotation/flip. Need to weight RNG to avoid identical-rotation clones
adjacent, OR enforce flip in 1-2 axes when an adjacent duplicate is detected.

### 9. Riparian woodland trees don't survive near rivers (80,50)
Trees in RIPARIAN_WOODLAND zone seem affected by floodplain mask in a way that
kills placement. Surface + vegetation layers are unaffected. Looks like
schematic placement is rejecting on floodplain or below-water-level.

## Per-tile feedback (S85 render)

| Tile | Action |
|---|---|
| **26,10** | Up tree density significantly in BT |
| **28,7** | Up tree + vegetation density in pine barrens (keep flowers). Rock gap too aggressive on shallow hills here |
| **17,41** | Up birch forest density |
| **20,36** | Birch clumps look good. Fix tree-clone rotation issue |
| **71,91** | Woodland savanna: too many big trees. **Generate cross-section PNG of all tree types down the middle** so user can pick which to keep. Then up density slightly. Temperate basaltic rock here looks GREAT used sparingly — keep |
| **40,28** | Limestone, looks good but too much rock gap. Need slope-mask exemption so flat plateaus get grass/soil |
| **38,11** | Too much limestone rock gap |
| **80,50** | RIPARIAN trees affected by floodplain mask → not surviving |

## Per-tile noted but no action

| Tile | Note |
|---|---|
| **15,61** | Lithology mismatch (granitic mask → basaltic game) |
| **38,15** | Lithology mismatch + too much rock |
| **89,52** | Lithology mismatch (deepslate → basaltic) |
| **10,77** | Basaltic, no good read |
| **17,66** | Spawned underground (stale teleport) |
| **86,75** | Ocean (stale) |

## Per-tile feedback batch 2 (S85 render walk continued)

| Tile | Biome | Action |
|---|---|---|
| **36,7**  | COASTAL_HEATH | More short grass; **double** the schematic-plant count |
| **19,23** | TEMP_RAINFOREST | GOOD baseline density. Ensure biome dither zones do NOT override floodplains |
| **32,10** | AT/SBT/BT transition | Density doubles in transition zones (BUG?). Up SBT density to match BT increases |
| **89,57** | BA | Up tree density. **Make BA more unique from BT/SBT** across tree mix + surface palette + vegetation — give suggestions |
| **33,13** | (cold biome) | Too barren — sparse bush schematics + significantly more short grass |
| **41,35** | TEMP_DECIDUOUS | **MAJOR**: NO trees generating. Maybe didn't refresh, but flag for investigation |
| **80,50** | RIPARIAN_WOODLAND | Floodplain mask is rejecting schematics (surface + veg pass). Schematic placement floodplain-exception broken |
| **33,49** | DRY_OAK_SAVANNA | Tree duplication (same rotation adjacent) + too much rock gap |
| **34,9**  | KARST | More veg + way more bushes (feedback re-iterated from earlier) |
| **50,48** | MIXED_FOREST | Up tree density. Rock gap too aggressive |
| **38,11** | CONT_STEPPE | Rock gap problem |
| **18,62** | DESERT_STEPPE_TRANS | More short grass. Tree dup issue. **CLARIFICATION**: "trees clustering" means ANY tree schems with no rotation variation, NOT just same-type |
| **36,75** | DRY_WOODLAND_MAQUIS | Up tree density slightly + more short grass + add bushes. Pine-leaf trees should be exceedingly rare |
| **8,73**  | FRESHWATER_FEN(?) | Transition-zone double density. **Palm trees** in FRESHWATER_FEN — palm trees should ONLY generate within ~30 blocks of coast |

## Stale validation tiles (CSV is wrong — actually ocean)

`memory/biome_reference_tiles.csv` is dramatically out of date. The following
tiles from the 36-tile validation set are actually OCEAN, not the labeled biome:

- **14,50** (labeled RAINFOREST_COAST)
- **72,92** (labeled EASTERN_TEMPERATE_COAST)
- **92,50** (labeled DRY_PINE_BARRENS)
- **86,78** (labeled SCRUBBY_HEATHLAND)
- **11,64** (labeled LUSH_RAINFOREST_COAST)
- **86,75** (labeled SEMI_ARID_SHRUBLAND)
- **43,89** (labeled TIDAL_JUNGLE_FRINGE)
- **32,89** (labeled MANGROVE_COAST)
- **17,66** (labeled SAND_DUNE_DESERT) — user spawned underground, "good" though
- **40,28** — user underground (stale teleport coord)

→ Regen CSV from current `masks/override.tif`.

## New architectural items discovered batch 2

- **Transition-zone density doubling**: at swap-zone pixels, schematic placement
  appears to roll the candidate list *twice* (once for each side of the
  boundary) — doubling density. Confirm in `core/schematic_placement.py:
  place_schematics` swap path. (Affects 32,10 and 8,73 visibly.)
- **Floodplain rejects schematics but accepts surface/veg**: in
  `core/schematic_placement.py`, the floodplain skip list is too broad —
  RIPARIAN_WOODLAND should NOT be in it (it's a flood-loving biome by name).
- **Biome dither overriding floodplains**: the ecotone swap shadow lookup may
  be picking neighbour-biome blocks even in floodplain pixels, breaking
  floodplain water-edge appearance.
- **Palm tree placement scope**: palm trees need a coast-proximity gate
  (within ~30 blocks of zone 0 ocean). Currently they generate inland in
  FRESHWATER_FEN.
- **Tree-clone rotation — broader scope**: ANY adjacent tree schematics with
  identical rotation count as duplicates, not just same-species. The fix
  weights RNG to roll different rotations for adjacent placements.

## Updated sequence proposal

**Phase 0 — Safety + diagnostics (do first):**
1. **Safety: cold-biome map compare** [DONE 2026-05-24]
2. **Regen `biome_reference_tiles.csv`** from current override — many tiles wrong
3. **Trace transition-zone density doubling** — possible swap-path bug

**Phase 1 — High-blast-radius architectural fixes:**
4. **Lithology mismatch audit** (15,61 / 89,52 / 10,77) — single stale-mapping bug
5. **Rock-gap slope re-clamp** — 8+ tiles flagged "too much rock"
6. **Wash intensification** (depth + threshold + width) — single change
7. **Tree-clone rotation fix** — schematic placement RNG weight
8. **Floodplain schematic exception** — RIPARIAN should not be in floodplain skip list
9. **Ecotone blob palette-correctness** — Option A shadow lookup picking wrong palette
10. **Water tick re-investigation** — S85 commit didn't fully land
11. **Palm tree coast-proximity gate**

**Phase 2 — Terrain shaping + visual:**
12. **Peak terrain crunch noise** — voxel-sniper-style /b e melt-style modifier
13. **Peak dirt scattering origin** — find what's putting dirt/mud at AT peaks (may keep)

**Phase 3 — Per-biome density tuning:**
14. **BT tree density up** (26,10)
15. **SBT tree density up** (match BT) (32,10)
16. **Pine barrens density + veg up** (28,7)
17. **Birch density up** (17,41 / 20,36)
18. **BA differentiation suggestion + impl** (89,57)
19. **Tundra/cold barren density up** (33,13)
20. **COASTAL_HEATH short grass + 2x schem plants** (36,7)
21. **DRY_OAK_SAVANNA density up + rock fix** (33,49)
22. **MIXED_FOREST density up + rock fix** (50,48)
23. **DESERT_STEPPE_TRANS grass up + rotation fix** (18,62)
24. **DRY_WOODLAND_MAQUIS density up + bushes + rare pine** (36,75)
25. **KARST veg + bushes** (34,9)

**Phase 4 — Diagnostics:**
26. **71,91 tree cross-section PNG** — let user pick which to keep

**Phase 5 — Investigations (no fix yet):**
27. **41,35 missing trees** — verify by re-render before reading deeper
28. **Stale references in CSV** — kill ocean tiles from validation set (covered by #2)

User to confirm or re-order.

---

## S86 validation-render walk feedback (2026-05-24 stopping point)

Render commit: `055b087` (had Phase 1A-I except 1F + Phase 3 + Phase 4 swap).
NOT in render: 1F-lite (`ec79dc7`), seam-fix (`2c4bf43`).

### Verified GOOD
- **(27,9) BA lowland** — pure, looks good.

### Per-tile new feedback

**(59,44) BT pure** — Lots of slope where trees would still grow. Need to either lower
tree slope cutoff OR raise `eco_placement.slope_penalty_start_deg` /
`slope_penalty_full_deg`. User asked if (30347, 22959) was floodplain — **NO**,
verified zero on hydro_floodplain + hydro_centerline + hydro_lake. Pure slope issue.

**(27,13) SBT** — (no comment, presumed pending more walk).

**(26,10) BT density** — Want MORE trees, FEWER bushes. **Bushes are overwriting trees?**
User hypothesis. Likely cause: BUSH_DENSITY_MULT (Phase 3C) + bushes-after-trees
stamp order in chunk_writer mean bushes can land on top of tree footprints.
Investigation needed: bush placement does NOT respect tree exclusion grid (only
its own bush_exclusion). Add cross-check.

**(32,10) BT/SBT transition** — Want MORE TALL trees, FEWER tiny trees.
Tree-size distribution within biome palette needs adjusting (favor lg/md over sm).
Also user notes no moss visible — BA palette moss_carpet add (Phase 3D) may not
be propagating to tile (32,10) because (32,10) is SBT/AT, not BA. The moss
addition was BA-only.

**(50,48) MIXED_FOREST** — (no comment, pending).

**(29,76) DRY_OAK_SAVANNA** — **WORSE on duplicates** (Phase 1D rotation tracker
failing). Possible causes:
1. _rotation_grid is per-tile, not per-pass-merged. Trees stamp first then
   bushes — bush rotation choice doesn't see tree rotations.
2. 4x4 cell grid is too coarse (or too fine) at savanna's typical tree spacing.
3. Two adjacent placements in same cell still get same rotation if the cell's
   "_used" set fills slowly.
Need diagnostic that prints per-cell rotation set after run.

### Major regression
- **River coming out as flat column.** Confirmed not from my chunk_writer Phase
  1G change (that only affects fluid_ticks NBT, not block placement). Possible
  causes: river_carver_v2 interaction with BT-banding override (unlikely),
  hydro mask sync between tiles, OR a pre-existing carve bug exposed by the
  re-render. Need to identify the affected tile coords + dump column to debug.
  **Worktree answer for user:** rendered from local repo
  `C:\Users\nicho\minecraft-worldgen` branch `s85-cherry-picks` at commit
  `055b087`. The `.claude/worktrees/` directories are stale agent scratch dirs,
  not in use.

### Reminders
- Check DRY_PINE_BARRENS (30,49) and (28,7) for plant rarity tuning.

### Carry forward
- 1F-lite (`ec79dc7`) and seam-fix (`2c4bf43`) commits exist but were AFTER
  render dispatch. Next render will include them.
- Phase 2A peak crunch still pending.

---

## S87 backlog seeded from S86 walk continuation

User-annotated my S86 carry-forward list (2026-05-25, walk #2). Items below
are CONFIRMED for S87 action with user-direction corrections folded in.

1. **Tree slope cutoff (59,44)** — Trees NOT growing on slopes where they
   should. LOWER the slope penalty threshold so trees accept STEEPER slopes
   too.  Files: `core/schematic_placement.py` slope_penalty_start_deg /
   slope_penalty_full_deg.  Direction confirmed.

2. **Bush-vs-tree cross-exclusion (26,10)** — Bushes ARE overwriting trees
   (user confirmed). Fix: bush placement must respect tree exclusion grid in
   addition to its own bush_exclusion.  Cross-check at bush placement site
   in `core/schematic_placement.py`.

3. **Tree size distribution (32,10)** — User CORRECTION: favor SMALL over
   LARGE at this tile (was my misread of "more tall trees" — user actually
   prefers small).  Tree-size palette weighting for SBT/transition needs
   smaller bias.  Per-pass-type size weighting in placement.

4. **Rotation tracker still placing duplicates (29,76)** — Phase 1D failing.
   Possible causes (need diagnostic):
   - 4x4 cell grid too coarse — make 2x2 or 3x3?
   - Tree+bush share grid OR not — clarify
   - Jitter (±3 blocks) is enough to land two placements in same cell with
     same rotation if cell's `_used` is empty

5. **River flat-column regression** — Confirmed real ("wtf is this"). NOT in
   my chunk_writer Phase 1G change (that only touches fluid_ticks NBT). Need
   to find an affected tile, dump a column at the river center, and trace
   carver path.  Suspects: river_carver_v2 lake-bowl interaction with new
   override.tif, OR a hydro mask sync edge case.

6. **DRY_PINE_BARRENS plant rarity (30,49 / 28,7)** — User wants to tune
   plant frequency.  Specific direction TBD on walk — check S86 backlog
   batch 1 for hints (28,7 said "Up vegetation density, but leave flowers").

(User continues walking — more items will arrive.)

---

## S87 walk #2 continuation (2026-05-25)

### Verified GOOD (no action)
- **(30,49) DRY_PINE_BARRENS** — River looks good here (no regression at this tile).
- **(36,75) MAQUIS** — Good baseline. Want MORE generic bushes.
- **(20,36) BIRCH** — Seems better (rotation tracker partially working).
- **(38,15) limestone rock-gap** — Yes (improvement working).
- **(33,49) DRY_OAK_SAVANNA** — Better.
- **Lithology mismatch** — Looks right across the board now. (Cloud lithology sync worked.)
- **Washes** — Beautiful.
- **Floodplain fixes** — Working.
- **Transition** — Good (where Phase 1F-lite isn't needed).

### Confirmed bugs / wishes from walk #2

7. **(28,7) sand blobs VISIBLE** — confirms 1F-lite (`ec79dc7`) is needed; will land next render.

8. **(34,9) KARST high-slope floodplain barren patch** — investigate: is gap==4
   (floodplain) firing on a steep slope here, killing schematic placement?

9. **(34,9) KARST GC — wrong grass type added** — I added `short_dry_grass` but
   user wants `short_grass`. **FIX**: revert my (34,9) KARST bump of
   short_dry_grass; bump `short_grass` instead. Same nuance applies anywhere
   I bumped short_dry_grass where user said "short grass" — audit all of
   Phase 3B for this slip.

10. **(40,28) limestone — needs Phase 2A peak-crunch noise to look natural.**
    Confirmed: 2A is now a real S87 priority (not deferred indefinitely).

11. **(38,11) STILL too much rock — needs sub-slope flat-detect.** User wants
    flat micro-areas WITHIN a macro-sloped tile to render as topsoil. Current
    rock_gap gate is per-pixel `cliff_deg`. Need: a SECOND pass that computes
    local-flatness around each pixel (e.g. 3x3 or 5x5 mean of `cliff_deg`).
    Pixels with low LOCAL slope variance but high per-pixel slope = bench/
    ledge → topsoil. Add `core/eco_gradients.py` post-pass.

12. **MAJOR — BA/BT/SBT palette reshuffle** (user definitive direction):
    - Remove **birches** from BA, BT, SBT entirely.
    - Move **BA's surface palette + tree mix + ground cover %** → BT.
    - After move: **cull tallest trees from BT** (BT becomes shorter than
      it was).
    - **Reduce big trees further in SBT** (SBT trees smaller than BT).
    - **Rule: higher elevation = smaller trees.** Apply consistently.
    - **More density across the board** in all three.
    - Net result: BA palette = old BA (shifted up the chain to BT, recolored?
      need clarification on whether BA gets a NEW palette OR if BA was meant
      to inherit from somewhere else).
    - Files: `BASE_DENSITY` + `GROUND_COVER_PALETTES` + tree-size weighting +
      `core/chunk_writer.py` BIOME_TO_MC + maybe schematic routing.

13. **Snow cover slope gate** — Default snow should NOT fall where slope is
    too intense, mirroring rock_gap intensity. Match the 35-45° fade band.
    Currently snow has SLOPE BIAS (steeper = more snow on ridges) but no
    HARD CAP at steep slopes. Add: where slope > X°, snow probability falls
    to zero (rock takes precedence). Files: `core/eco_gradients.py` snow
    gap section (~line 533+).

14. **(80,50) RIPARIAN trees killed by biome DITHERING (not floodplain)** —
    User hypothesis: trees are interspersed with adjacent biome via ecotone
    swap, then that swap routes to a biome that REJECTS placement there.
    **Fix**: add `RIPARIAN_WOODLAND` to `_NO_SWAP_BIOMES` in
    `core/schematic_placement.py` (line ~893). RIPARIAN opts out of ecotone
    swap so its trees survive.

15. **(8,73) STILL inland palms** — Specific palm with **stripped jungle wood
    as its log**. User: get rid of it entirely OR gate to 4 blocks of water.
    Need to grep schematic_index.json for palm variants with jungle log
    block. May not be classified as `rfpalm/mpalm/cpalm` — that's why Phase
    1H gate doesn't catch it. Identify species + add to filter.

16. **MANGROVE_COAST** — Way more vegetation; density up on trees too; reduce
    scale of surface block noise.
    - `BASE_DENSITY["MANGROVE_COAST"]` 0.08 → 0.14
    - GC palette: add more entries / bump existing
    - Noise scale reduction (config knob)

17. **(51,53) river regression** — Something happening, observation pending.
    Possibly same root cause as (33,7) chunk error below.

18. **(33,7) rivers MOSTLY good — slight block updates over one chunk** —
    Phase 1G water-tick fix is mostly working. Remaining: single chunk has
    leak. Could be:
    - One chunk where river_water_y is 0 (so river check fails) but block
      is still water — Y >= SEA_Y catches it normally, but if at SEA_Y it
      still ticks.
    - Or a chunk where my tile-local index conversion goes wrong at chunk
      edges.

(User still walking — more items pending.)

### Clarifications received

**Birch removal scope:** Only birch species that were added to BA/BT/SBT today
("we added"). User says "Anything we added when you suggested adding birches
for diversity to BA or BT". My memory: I planned Phase 3D as "no birches" and
the implementation only added `moss_carpet` to BA GC + MC tag swap. I did NOT
add birch SCHEMATICS. Possible sources:
- BT's existing `btaiga_tree_*` routing has a birch in it natively
- SBT mirror to BA propagates an existing birch
**Action**: grep `schematic_index.json` for birch entries routed to BT/SBT/BA;
filter them out via post-load `grouped[*]` filter like SARID/MAQUIS pattern.

### Mid-render regressions caught 2026-05-25 (Vandirtest11 walk)

**Render killed mid-flight** (commit `a89cf3a` had Phase 2A rock-gap crunch).

19. **Trees floating on slopes again** (REGRESSION from Phase 2A).  Phase 2A
    displaces `surface_y` by ±2 blocks at `gap_mask == 5` (rock_gap) pixels
    AFTER decorate_surface but the displacement is applied BEFORE schematic
    placement reads surface_y for anchoring.  Or — wait — `place_schematics`
    is called AFTER `decorate_surface`, so it should see displaced surface_y.
    Hypothesis: schematic_placement.py is reading the ORIGINAL surface_y from
    column_generator (line 191 in run_pipeline) for some calculation path,
    NOT the modified one after Step 6e.  Trace: which surface_y does
    `place_schematics` actually use?  Investigate before re-dispatching.
    Possible fix: apply 2A displacement BEFORE column_generator returns OR
    plumb the displaced array through everywhere.

20. **Snow cover overrides tree anchoring 6 blocks down** (NEW).  Schematic
    placement Y is computed but then snow_carpet / snow_block places ON TOP
    OF the placed tree's surface position, burying the tree's anchor.  Need
    to either:
    - Place snow BEFORE schematic placement so schematics anchor on snow
      surface, OR
    - Skip snow placement at pixels where a schematic was placed.

(Both must be fixed before next render dispatch.)

---

**BA/BT/SBT palette reshuffle (FINAL spec, user-confirmed):**

Mode: **(b) ADDITIVE**. BT/SBT keep their native species, gain BA's species,
then cull by height.

- **BA**: unchanged.
- **BT**: native `btaiga_*` species PLUS BA's tree pool (the SBT-mirrored set
  with any S87 birch-removal applied). Then **drop trees > 25 blocks tall**.
- **SBT**: native `sbtaiga_*` species PLUS BA's tree pool. Then **drop trees
  > 20 blocks tall**.

Result: BT shorter than its current self (lost the tall conifers), SBT
shorter still. "Higher = smaller trees" rule applied via the height cuts.

Scope (user-confirmed):
- **Trees only** — merge BA's tree pool into BT/SBT routing, then cull.
- **Surface blocks**: copy **BA's surface palette to BT and SBT** (overwrite).
  BA's surface palette is "better" per user.
- **Ground cover (vegetation)**: leave each biome's current GC palette
  **unchanged**.

Implementation:
1. Post-load merge in `core/schematic_placement.py` (~line 326-357 pattern):
   - `grouped["BOREAL_TAIGA"] += [e for e in grouped["BOREAL_ALPINE"] if e.schem_type=="tree"]`
   - Same for `SNOWY_BOREAL_TAIGA`.
2. Height cull via schematic metadata if available, else by parsing path
   prefix (e.g. `_lg` / `_md` / `_sm` size code → approximate height).
   - Need: a height-by-species lookup. May already exist in `schematic_index.json`
     metadata (`height`, `lowest_leaf_y`, etc.). Confirm before coding.
3. Surface palette overwrite in `core/surface_decorator.py`: BT and SBT surface
   block decisions route through BA's palette config.
