# S85 Handoff — 2026-05-22/23

## Tldr

S85 was a **reconciliation session**. Local master had been stuck at S69 while origin/master quietly advanced through S70-S84 over the course of weeks. Hand-merging had introduced a Frankenstein state in the working tree (S69 base + selective S84 patches like the terrain spline) that nobody could fully audit. Today's work: surfaced the divergence, picked which S70-S84 work to bring in (most of it), layered surgical S85 edits on top, and scaled all stale world-height thresholds for the 768-block world.

Final state lives on branch `s85-cherry-picks` (not yet merged to master, awaiting in-world validation render).

## What was wrong before S85

- Local master: `5d13727` (S69)
- `origin/master`: `c86334c` (S84) — **32 commits ahead**
- A previous Claude instance had hand-applied select S84 patches into the local working tree (terrain_spline, Y_MAX 704, scipy import in chunk_writer) but never `git pull`-ed.
- Today's CLAUDE.md handoff claimed master was at S84 but actual `git log` showed S69. Misleading.
- The S83 cloud bake was actually run from `origin/master`, which had all the S70-S79 biome work + S80-S83 river polish. So the "it's perfect" bake state was real — but our local edits were against S69, drifting from canonical.

## What S85 landed

### Cherry-picks from S70-S84 (via `git checkout origin/master -- <file>`)

| ID | Item | Origin |
|---|---|---|
| A1 | bbox-cull spline polygons in `_rasterize_river_edges_tile` | S84 perf — 3.3× speedup on land tiles |
| A2 | `vandir_height.zip` auto-install in `run_pipeline.py` | S74 |
| A3 | AT high-elev stone-fade EXEMPTION + fade band 500-580 (S85 bumped from S84's 480) | S71 + S85 |
| A4 | AT GC palette: `dead_bush` → `tall_dry_grass` | S70 follow-up |
| A5 | `tools/diag_mca_biomes.py` + `tools/diag_mca_surface.py` | S71 |
| B1 | FROZEN_FLATS Tundra Valley redesign (grass_block surface, badlands MC, scattered snow_carpet, sparse pines) | S71-3 |
| B2 | Lithology mask as source-of-truth for cliff stone + high-elev fade (per-biome `zone_to_group` is FALLBACK) | S71 |
| B3 | Ecotone plateau-clamp dither — S85 widened to width 100, swap_cap 0.85 (was 40/0.75 in S71) | S71 + S85 widening |
| B4 | Schematic placement `BASE_DENSITY` full retune | S70-S81 walks |
| B5 | Leaf-column blacklist (4 schematics globally rejected) | S71 |
| B6 | SARID juniper-only filter (acacia variants excluded) | S71 |
| B7 | BIRCH clumping fix | S70-f6 |
| B8 | Floodplain skip for RIPARIAN_WOODLAND, FRESHWATER_FEN, LUSH_RAINFOREST_COAST, SAND_DUNE_DESERT | S70 |
| B9 | AT + FF added to `_NO_SWAP_BIOMES` (no neighbor big-tree leak) | S71 |
| (river) | S71-S83 river overhaul: WP-style guardrails carve, tanh depth, coast taper, lake-bowl geomorph | S71-S83 |
| (cache) | Disk-pickle bed cache for multi-worker memory headroom | S83 v18 |

### S85 surgical edits (re-applied on top)

- **BOREAL_TAIGA** MC tag `stony_shore` → `minecraft:meadow` (user override of S71 decision — meadow temp=0.5 keeps it snow-free and reads cleaner than stony_shore which had temp=0.2 freeze-risk)
- **`BIOME_ALTITUDE_REMAPS = []`** — deleted the remaining BIRCH/MIXED → BOREAL_ALPINE entries at threshold=220. User direction: "i dont like those remaps. they dont make any sense to me. DELET". Runtime code at chunk_writer.py:~1607 short-circuits cleanly on empty list.
- **Snow gap==7 surface override exemption** for SBT + FROZEN_FLATS — surface_decorator.py:1690 now skips snow_block override for those two biomes. SBT keeps native podzol (snow_carpet via `_apply_snow_carpet` provides snowy visual); FROZEN_FLATS keeps Tundra Valley grass_block.
- **ARCTIC_TUNDRA → SBT below Y 500 remap DELETED** — surface_decorator.py:~2326. Painted intent is canonical. Unlocks lowland ARC_TUN tiles like (31,5).
- **A3 fade band start** bumped 480 → 500 per user direction (keeps mid-mountain forests green longer).
- **B3 ecotone wider** — config/thresholds.json `eco_ground_cover.ecotone_width_px` 40 → 100, `ecotone_swap_cap` 0.75 → 0.85. User: "incredibly wide … biomes almost become each other."
- **Treeline rescale** for 768-height world (full table in CLAUDE.md and `config/thresholds.json:treelines`).

### World-height threshold scaling for 768-block world

These were stale at 448-era values even in origin/master S84. S85 scaled them:

| Constant | Was | Now | File |
|---|---|---|---|
| SNOW_Y_FLOOR | 250 | 430 | core/eco_gradients.py:498 |
| SNOW_Y_CEIL | 275 | 475 | core/eco_gradients.py:499 |
| GRASS_Y_FLOOR | 325 | 460 | core/surface_decorator.py:1415 |
| GRASS_Y_CEIL | 350 | 500 | core/surface_decorator.py:1416 |
| EXPOSED_MIN_Y | 180 | 310 | core/layers/pass2_surface/weathered_top.py:41 |
| `_default` treeline fallback y_top | 230 | 530 | core/schematic_placement.py |
| preview_renderer normalize range | 448 | 704 | core/preview_renderer.py:138, 180 |
| Stale comments (chunk_writer) | "32 sections / Y 447" | "48 sections / Y 703" | core/chunk_writer.py:1520, 1814 |

Plus the full per-biome treeline rescale in `config/thresholds.json:treelines`.

### Datapack rename

`assets/vandir_height.zip` (the older 512-block-height version) → renamed to expose `vandir_world_v17_S84_height768.zip` as the new `vandir_height.zip`. The S74 auto-install in `run_pipeline.py` references `vandir_height.zip` by hardcoded name, so this rename activates the 768-block datapack. Old datapack preserved at `.claude/S85_preserved/vandir_height.zip` for rollback.

## What was NOT brought in

- Connectivity layer (S75-S79 archived behind `_CONNECTIVITY_ENABLED = False` — needs ground-up rebuild)
- S70-S71 `BIOME_TO_MC` remaps for biomes other than BOREAL_TAIGA (BT was user-overridden to meadow; the rest came in unchanged)

## Validation render plan

36-tile validation set at [cloud_bake/validation_tiles.txt](../cloud_bake/validation_tiles.txt). Estimated 12-15 min wall on 1× CCX63 (~$0.14) or 4× CCX63 for memory safety (~$0.57).

See user-facing snapshot-refresh + render procedure in CLAUDE.md (end of session). Snapshot the staging box AFTER pulling s85-cherry-picks branch + smoke-testing (51,53), then spin workers from the new snapshot.

## Carry-forward to S86

1. **Validate the 36 tiles in-world.** Walk each. The wider ecotone (100 blocks) is the biggest cosmetic risk — if it looks excessive, drop to 60-80 and re-render.
2. **Merge `s85-cherry-picks` → master** after validation passes.
3. **Refresh cloud snapshot** as `vandir-baked-s85` so future renders pull from this state.
4. **Connectivity layer rebuild** — S79 archived this. Still TOP PRIORITY for proper river connectivity in dry biomes per the S77+S78+S79 commit message.
5. **A2 datapack name hygiene:** consider unifying on a single, dated zip name (e.g. `vandir_height_h768_v1.zip`) and updating the S74 auto-install path to read whichever file matches `vandir_height*.zip` rather than hardcoded name. Easy follow-up.
6. **B3 width** is currently 100. If "transitional biomes" reads great, leave it. If overdone, dial down. If user wants even bigger (the original ask was "incredibly wide"), bump to 150-200.

## Files touched

- `assets/vandir_height.zip` — renamed in
- `cloud_bake/validation_tiles.txt` — new
- `config/thresholds.json` — treelines rescale, ecotone widen
- `core/chunk_writer.py` — BT MC tag, REMAPS=[], stale comments
- `core/column_generator.py` — origin/master version
- `core/eco_gradients.py` — origin/master + SNOW_Y rescale + floodplain skips
- `core/hydro_region_overlay.py` — origin/master (incl. bbox-cull)
- `core/hydrology_precompute.py` — origin/master
- `core/layers/pass2_surface/weathered_top.py` — EXPOSED_MIN_Y rescale
- `core/preview_renderer.py` — Y range 448→704
- `core/river_carver_v2.py` — origin/master
- `core/schematic_placement.py` — origin/master + treeline default rescale
- `core/surface_decorator.py` — origin/master + S85 surgical edits + GRASS_Y rescale + A3 fade band
- `run_pipeline.py` — origin/master (incl. datapack auto-install)
- `tools/diag_mca_biomes.py` — new
- `tools/diag_mca_surface.py` — new
- `memory/S85_handoff.md` — this file
- `CLAUDE.md` — current-state header rewrite

## Commits

- `a738fb4` S85 WIP: snapshot before bringing in S70-S84 cherry-picks
- `8dca21b` S85: cherry-pick S70-S84 work + S85 surgical edits + world-height threshold scaling

## Reference

- `.claude/S85_pre_pull_diff.patch` — full patch of S69 → Frankenstein state, for rollback / archaeology
- `.claude/S85_preserved/` — backups of conflicting untracked files (old vandir_height.zip etc.)
