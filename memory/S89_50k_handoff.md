# S89 → 50k REGEN handoff — **COMPLETE** (2026-06-04)

## ⚠️ KNOWN DEFECT — RE-RENDER PENDING (2026-06-05)
The 50k render has a **missing-chunk bug along all water-bearing tiles**: wherever a
river/lake/ocean touches a TILE boundary, the perimeter chunk(s) there are absent from
the .mca → MC backfills them with the flat-ocean generator → **rectangular Y=63 ocean
squares + "swimming-pool wall" cliffs cut into rivers**, tracing the hydrology along the
512-block tile grid. Sample scan: 212 missing land chunks in an 81-region area (all on
tile-perimeter coords).

**ROOT CAUSE (fixed in code, NOT yet re-rendered):** `core/chunk_writer.py`
`_add_water_ticks` (nested in `_chunk_to_nbt_bytes`) referenced `river_water_y` as an
out-of-scope free var → `NameError` on every tile-edge chunk with edge water → swallowed
by the per-chunk `except Exception: continue` (line ~2427) → chunk dropped. `run_pipeline`
only counts tile-level success, so the render reported "9409/9409, 0 errors" while silently
dropping these chunks. My post-render verify also missed it (checked files exist + present
chunks decompress; did NOT check for MISSING inland chunks).

**FIX (committed):** threaded `river_water_y` as a real parameter:
`write_tile` → `write_tile_to_region` → `_chunk_to_nbt_bytes` → `_add_water_ticks`
(4 edits + de-stale the misleading comment). Compiles; verification render in flight.

**TO ACTUALLY FIX THE WORLD:** must RE-RENDER (terrain only exists if the pipeline makes it).
Affects ~all water-bearing tiles → effectively a full re-render. User chose **HOLD FOR NOW**
(2026-06-05) — boxes were destroyed; re-render later via `render_50k.sh` with the fix.
When re-rendering: (a) use parallel tar-over-ssh collect from the START (the script's
sequential `scp … || true` collect hung pathologically — see COLLECT GOTCHA below);
(b) NEVER run a backup / heavy disk job on the SSD while MC is open (it hung a save → watchdog
crash); (c) ADD a post-render verify that scans for MISSING inland chunks, not just file count.

## STATUS: 50k REGEN RENDERED & WALKABLE — but needs ONE re-render for the river-chunk fix
The S89 surface overhaul shipped and the full 9,409-tile world regen is collected, assembled,
and walkable (`D:\modrinth_vandir\saves\Vandir50k`, "Vandir 50k" in the Fabric 26.1 profile).
Endless-ocean generator + west-coast spawn (7680,65,25740) are set.

## RE-RENDER PUNCH-LIST (batch ALL of these into the one re-render)
1. **[DONE in code] Missing river-edge chunks** — `river_water_y` threaded through the writer
   (see defect section above). Verify with a missing-inland-chunk scan after re-render.
2. **[TODO] Rock-relief TILE seam** (`core/surface_decorator.py:_apply_rock_relief`, ~2974).
   The relief noise is world-coord (seamless), BUT two amplitude terms are per-tile / biome-keyed
   and step at boundaries: (a) `smooth_gain` = `|sy - gaussian_filter(sy, σ4)|` uses a PER-TILE
   gaussian → edge-truncation → thin seam at tile borders within rock; (b) the snow-zone `_pk`
   relief weights by `_build_snow_line(biome_grid)` which JUMPS at biome boundaries (e.g. snowy_taiga
   vs the granitic highland at tile 73/74,66 — user-reported). Both mutate surface_y → visible seam.
   FIX: compute `smooth_gain`'s gaussian on a PADDED surface_y (halo), and smooth/continuous-ize the
   snow_line transition so `_alt_w` doesn't step. Verify via top-down render straddling a tile+biome edge.
3. **[TODO] Krummholz (schematic) NOT padded** — `core/schematic_placement.py:1306` is explicitly
   "Inner-only (no padding)"; krummholz density `_kr_dens` derives from a within-tile distance
   transform (`_kr_dist`, ~1390) → density seam at tile edges. FIX: promote the schematic/krummholz
   pass to padded (halo) like the S58/S59 ecotone padding (`biome_grid_padded` infra). Verify across a tile seam.
4. **[TODO] Relief seams at EVERY biome-relief boundary** (user: "seam lines are EVERYWHERE there is
   biome relief change"). The snow-zone `_pk` relief weights by `_build_snow_line(biome_grid)` which steps
   per-biome → surface_y step at every biome boundary. FIX: make the snow_line spatially CONTINUOUS
   (smooth/blur the per-biome snow_line field) so `_alt_w` ramps instead of stepping; de-couple relief
   amplitude from biome identity generally.
5. **[TODO] Relief too weak on GRADUAL rock** (user: "fire the slight relief higher at gradual rock slopes;
   shouldn't be near-zero at its lowest bounds"). `slope_gain_by_tier=[0.25,0.6,1.0]` makes tier1 (gentle
   rock 40-45°) near-zero. Raise tier1 gain (e.g. 0.25→0.55+) so gentle rock gets visible relief.
6. **[TODO] Steep slopes get NO rock exposure** (user: slope mask is PRE-spline). TWO rock systems:
   (a) `rock_layers` tiers/relief ARE post-spline (`build_terrain_derived.slope_deg_from_surface_y` runs on
   MC-Y heights) BUT at 1:8 with gaussian smoothing → steep NARROW faces average below the 40° tier1 thr →
   no rock; (b) gap==5 "surface→stone" `rock_gap.tif` is from Gaea `slope.tif` = PRE-spline Gaea-normalized
   (user's hypothesis is right for THIS one). FIX: re-derive rock_gap against post-spline slope, and/or lower
   tier thresholds, and/or build rock_layers at 1:4 not 1:8 to catch steep faces.
7. **[TODO] Krummholz OVER-firing** (user: "snowy taiga zones at high elevations are being turned ENTIRELY
   into krummholz"). Should fire ONLY on (a) very steep slopes OR (b) the NORTHMOST/treeline-edge ends of
   snowy taiga. Tighten gating in `schematic_placement.py` (~1333-1420): gate krummholz on high cliff_deg
   OR treeline-proximity, not blanket high-elevation SBT.
8. **[TODO] Snowy-taiga plateau edges get NO TREES** (user: likely overridden by snow cap in ARCTIC_TUNDRA
   edge zones on the plateau). Investigate snow-cap (cliff_cap / snow) tree-suppression dilation eating the
   tundra/taiga plateau treeline.
9. **[TODO] Litho tier (dark/mid/light) transitions should be SMOOTH GRADIENTS where rock touches rock**
   (user) — currently hard slope-threshold buckets (40/45/50°). Blend tier palettes gradually where rock
   meets rock; keep hard only at the rock/non-rock ends. (surface_decorator rock painting.)
10. **[TODO] Surface litho-GROUP boundaries should gradient-fade into each other** (user) — where two painted
   lithology groups meet (granitic↔basaltic etc., from `masks/lithology.tif`), the per-group rock palettes
   switch ABRUPTLY → hard marked line. Blend them across a transition zone (salt-and-pepper dither across a
   ramp, same pattern as the S58/S59 biome ecotone dither, but on the lithology-group palette selection in
   surface_decorator). Pairs with #9 (tier blend). Keep group fade ONLY where rock touches rock.
11. **[TODO] River containment walls too aggressive in tall terrain** (user) — in tall areas the river
   "containment"/berm raise shoots straight up as a 90° L wall RIGHT at the waterline → no exposed riverbed.
   User: keep cliffs around rivers, but there should ALWAYS be a curved riverbed/bank exposed to the water.
   LIGHTWEIGHT FIX: in the berm/wall-raise pass (run_pipeline ~1048-1160, `_water_mask_for_berm` /
   `_nearest_water_y`), keep a 2-3 block SHORE BUFFER adjacent to river water at/near water level (sloped
   curved bank), and only raise the containment wall BEYOND that buffer — so the cliff is SET BACK and a
   curved bank is always exposed. Don't kill the cliffs, just inset them from the water.
12. **[TODO] Some lakes render DRY** (user: "I don't see any lakes"). 117 lakes in `hydro_lake.tif`;
   deployed-world spot-check: WATER OK at tile(20,75)=`10448,95,38528` & tile(33,33)=`16944,78,17376`,
   but DRY at tile(81,63) (lake_wl→Y103 vs basin floor Y105 — not carved below water level) and tile(28,15)
   (bogus spill Y470 vs terrain Y260 — bad spill-point elevation). TWO sub-bugs in hydrology_precompute /
   carver: (a) lake basin not carved/deepened below its water level → no depression to hold water; (b) bad
   spill-elevation computation (too high) at some high-altitude basins. `hydro_lake_wl.tif` is NORMALIZED
   [0,1] → ×65535 → spline → MC-Y (verified: matches working lakes). Quantify dry-rate across all 117 +
   fix carve/spill before re-render.
NOTE: user is finding these by walking; expect MORE punch-list items before the re-render fires. Batch ALL,
verify across seams with top-down renders, THEN one re-render.

- **Render commit:** `51d41f4` (s85-cherry-picks). All 8 boxes verified on it pre-render.
- **Result:** 9,409 / 9,409 MCAs, **0 render errors**, 66 GB.
- **Verified:** perfect 97×97 grid (rx 0..96 × rz 0..96), every row exactly 97 files,
  0 zero-byte/truncated files.
- **MCAs live at:** `D:\Vandir50k\region\` (PortableSSD).

## THE WALKABLE WORLD (D:\Vandir50k)
Assembled and junctioned into the Modrinth `test` profile so MC sees it:
- `D:\Vandir50k\region\`   — 9,409 MCAs
- `D:\Vandir50k\level.dat` — cloned from Vandirtest10 (matched pair w/ datapack),
  renamed **LevelName "Vandir 50k"**, GameType 1 (creative), allowCommands 1,
  DataVersion 4556, spawn moved off 0,0,0 → **26368/150/27392** (tile 51,53 showcase:
  floodplain/lakes/mixed forest). DataPacks.Enabled = `vanilla`, `file/vandir_height.zip`.
- `D:\Vandir50k\datapacks\vandir_height.zip` — **the 768 datapack** (md5 `edd109ad`).
- **Junction:** `…\ModrinthApp\profiles\test\saves\Vandir50k` → `D:\Vandir50k`
  (so MC reads/writes straight off the SSD; shows as **"Vandir 50k"** in the list).
- To walk: open Modrinth `test` profile → world **"Vandir 50k"**. Spawns over tile
  (51,53); fly/`/tp`. Other validated TPs in CLAUDE.md (e.g. `/tp @s 18432 200 10240`).

### DATAPACK RESOLUTION (the "right vandir_height.zip" question — SETTLED)
Read the actual `dimension_type` height out of each zip; do NOT trust filename/labels:
- `edd109ad` (Vandirtest10 active + `.claude/S85_preserved/`) → **height 768** ✅ USE THIS.
  Proven: it's what the validated S89 768-height tiles were walked with; matched pair
  with the level.dat we cloned.
- `assets/vandir_height.zip` (`c133c2bc`) → also height 768, BUT additionally ships a
  `data/minecraft/dimension/overworld.json` that overrides the noise generator. Valid,
  but not the matched pair for Vandirtest10's level.dat — avoided to dodge antipattern #7.
- `…/vandir_height.zip.bak_pre768` → **height 512** ❌ would clip everything above Y447
  (our peaks reach ~Y700). Never use.

## HOW THE RENDER RAN (for the record)
- **Calibration** (box 78.47.145.92, full z=50 row, ~50% land): mask build 450s one-time/box,
  render 1050s/97 tiles → **5.54 tiles/min/box**, 0 errors. Locked **THREADS=40 OMP=1**.
- **Fire:** `OUT_DIR=/d/Vandir50k/region THREADS=40 OMP=1 bash cloud_bake/render_50k.sh <8 IPs>`
  (run_in_background, NO trailing `&`). render_50k.sh OUT_DIR is now env-overridable.
- **8 boxes**, round-robin z-rows (box b: rows b, b+8, …). 7 boxes finished ~T+200m clean.
- **b1 straggler:** drew land-heavy rows (some 27–29 min/row), lagged to 916/1164. Stopped it,
  **redistributed its remaining rows 73/81/89 to idle boxes b0/b2/b3** (tmux `r50b`), then
  touched b1 `/root/r50_done` to trigger collect. Cut ~65 min solo → ~30 min.

## COLLECT GOTCHA (hard-won — fix the script before next time)
- render_50k.sh's built-in collect = **sequential** `scp -q …/r.*.mca …  2>/dev/null || true`.
  The single-session **big-glob scp (~1358 files) HUNG / crawled** (~4 files/min) AND the
  `|| true` silently swallows failures. Pathological for thousands of files.
- **Replacement (worked great):** parallel **tar-over-ssh**, all 8 boxes at once:
  `ssh root@IP "tar cf - -C /root/minecraft-worldgen/output ." | tar xf - -C /d/Vandir50k/region/`
  ~19 MB/s aggregate (download-bound), 66 GB in ~103 min.
- **Row-73 overlap:** b0 had the authoritative full row 73, b1 a partial. Excluded
  `--exclude=*.73.mca` on b1's tar to avoid two streams writing the same files. Result: exactly 9409.
- **TODO for render_50k.sh:** replace the collect loop with parallel tar-over-ssh + drop the
  silent `|| true`; verify count == expected at the end.

## NEW / CHANGED TOOLING THIS SESSION
- `cloud_bake/collect_monitor.ps1` — live SSD collect monitor (files/9409, GB, MB/s, ETA, done-beep).
- `cloud_bake/monitor.ps1` — fixed two bugs: `-Ips` self-splits a comma string; renamed the
  `$stallMinutes` hashtable → `$stallTracker` (collided with the `$StallMinutes` int param).
- `cloud_bake/render_50k.sh` — `OUT_DIR` made env-overridable.

## REMAINING / NEXT
- **User destroying the 8 boxes** (safe — world fully collected + verified).
- Walk "Vandir 50k" in-world for a final eyeball.
- (Background, optional) server-side `frozen_peaks` weather test.
- Fold the collect fix into render_50k.sh (see TODO above) for the next regen.
