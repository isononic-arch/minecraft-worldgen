# Vandir Audit — 2026-04-10 (post-Session-41 handoff)

Read-only pass. Nothing moved or deleted. Every recommendation requires your approval.

---

## Phase 3 — Validation script audit

**Finding: you already have a working validator.** The handoff doc assumed `validate_heightmap/chunks/biomes.py` don't exist and need to be built. They don't need to be built — `tools/validate_test_tile.py` is 923 lines, registers 10 checks, exits 0/1/2, emits `checks.json` + PNG reports, and last ran green (9 PASS / 1 WARN on tile 36,20).

Existing checks, by category:
- terrain: `surface_y_range`, `water_fill`, `bedrock_layer`, `no_void_columns`, `column_profile`
- biome: `biome_coverage`
- hydrology: `river_meta_consistency`
- surface_decoration: `no_bare_dirt_surface`, `surface_block_variety`
- vegetation: `schematic_placements`

CLI: `python tools/validate_test_tile.py --config config/thresholds.json --masks masks/ --output output/ --tile-x 36 --tile-z 20 --report validation_report_36_20 [--dry-run]`

**Real gaps (what's actually missing for agentic loops):**

1. **No multi-tile runner.** Validator is single-tile. For "did this change break anything?" loop you need a 3×3 wrapper (e.g. `tools/validate_3x3.py`) that runs 9 tiles (or the 7 canonical reference tiles from CLAUDE.md) and aggregates PASS/FAIL. Non-trivial: ~60s × 9 tiles = 9 min, needs parallel-dispatch like `run_pipeline.py`.
2. **No mask-level validator.** Nothing checks `masks/*.tif` for sanity (dtype, bounds, histogram class balance, NaN count, expected coverage %). After a `rebuild_*.py`, the only way to know it's sane is to generate a tile and look. Proposed `tools/validate_masks.py` with per-mask class: file exists, dtype matches expected, coverage within `[min%, max%]` from `config/thresholds.json`.
3. **No top-down renderer wrapper.** Rule says "render 3×3 top-down before generating .mca." There are ~8 `diag_*topdown.py` scripts doing this ad-hoc but no canonical entry point. Propose `tools/render_topdown.py --tile X Z [--3x3] [--world]` that wraps the existing diag logic.
4. **No machine-checkable "is this .mca loadable" test.** Once .mca is written, the only verification is "open Minecraft." Propose lightweight check using nbtlib to parse each chunk and assert section count, biome palette min_bits=1, no Y>255 silent drop.
5. **No regression harness.** A validator is useful only if prior runs are snapshotted. Propose `validation_report/baseline/` + diff against current in the 3×3 runner.
6. **`diag_*.py` scripts don't report PASS/FAIL** — they produce images for humans. Not validators. Grep confirmed: only `diag_floodplain_options.py` mentions PASS/FAIL text.

**Recommended sequence** (easiest first): #3 (1 day) → #2 (1 day) → #1 (2 days) → #4 (1 day) → #5 (half day).

---

## Phase 4 — Agentic prep

`.claude/` exists but contains only `settings.local.json` (14 KB). No `skills/` dir.

**CLAUDE.md gaps for autonomous runs.** It's thorough on rules but thin on *how to actually do things*. A cold Claude session cannot currently run a test render from CLAUDE.md alone because it lacks:

- Exact command to run the tile validator (shown in Phase 3 above — add to CLAUDE.md)
- Exact command to rebuild each mask (e.g. `python rebuild_sand_dunes.py` — what args? stdout? writes where?)
- Exact command to run `run_pipeline.py` for a single tile vs 3×3 vs full
- Where chunk output goes vs where it must be copied (`output/` → `C:\Users\...\Vandirtest10\region\`) — documented as a file path in CLAUDE.md, but no copy command shown
- How to read `checks.json` as PASS/FAIL
- How to interpret `step0_output.json`
- How to read `preview_log.txt` / `chunk_errors.log`
- What "the diagnostic passed" looks like for each `diag_*.py`

**Proposed skills to draft** (in `.claude/skills/`):

| Skill | Purpose | Core loop |
|---|---|---|
| `debug-render` | Reproduce a rendering bug on one tile | validate_test_tile → open report → compare against baseline |
| `fix-and-verify` | Apply a code change then prove it didn't regress | edit → rebuild relevant mask → 3×3 validate → diff baseline |
| `rebuild-mask` | Regenerate one gradient mask with threshold change | edit config/thresholds.json → run rebuild_*.py → validate_masks → render top-down |
| `new-palette-layer` | Add a layer following Physical Realism pattern | read _apply_desert_rock_palette → scaffold new function → verify last-step rule → validate 3x3 |
| `seam-investigation` | Debug tile-boundary artifact | tools/check_tile_seams → classify staircase → match to solved-problems index |

I have NOT created these yet — waiting on your approval and on the 3×3 validator existing (skills reference it).

**Smaller CLAUDE.md additions I'd recommend** (one-line each, append to the end):
```
### Commands
- Validate one tile: python tools/validate_test_tile.py --config config/thresholds.json --masks masks/ --output output/ --tile-x X --tile-z Z --report validation_report_X_Z
- Rebuild a mask: python rebuild_{name}.py  (each writes masks/{name}.tif, logs to stdout)
- Generate one tile MCA: python run_pipeline.py --tile X Z (writes output/r.rx.rz.mca)
- Copy to test world: cp output/r.*.mca 'C:\Users\nicho\AppData\Roaming\ModrinthApp\profiles\test\saves\Vandirtest10\region\'
- Interpret validation: checks.json -> {"passed": N, "failed": N}; exit 0 = green
```

---

## Phase X — Root cleanup proposal

Root currently has **35 `diag_*.py`, 14 override_*.png duplicates, 5 `rebuild_*.py`, 11 `validation_report*` dirs, plus top-level `app.py`, `paint_override.py`, etc.** It's not a repo, it's a scratchpad.

**Proposed structure** (no moves yet — awaiting approval):

```
minecraft-worldgen/
├── core/              (keep as-is, it's already clean)
├── tools/             (keep — GUI + validator)
├── config/            (keep — thresholds.json only)
├── masks/             (keep)
├── output/            (keep, gitignored)
├── pipeline/          (empty — delete or populate)
├── scripts/
│   ├── rebuild/       ← move 5x rebuild_*.py from root
│   ├── diagnostics/   ← move 35x diag_*.py from root (further triage below)
│   └── one_off/       ← step0_diagnostic.py, scan_schematics.py, merge_anchor_index.py,
│                         rename_vegetation.py, height_polarity_check.py, convert_masks.py,
│                         schem_viewer.py, validate_schematics.py, paint_override.py,
│                         upscale_override.py, upscale_override_vectorized.py, river_pass.py,
│                         generate_lake_wl.py, app.py, panel4_override_painter.py
├── docs/              ← PROJECT_MEMORY.md, ARCHITECTURE_VISION.md, MASK_PIPELINE_REFERENCE.md,
│                         PLACEMENT_VARIATION_SPEC.md, VEGETATION_MIX_SPEC.md
├── archive/           ← old override_*.png, "- Copy" files, validation_report_* stale dirs
├── .claude/skills/    ← new
├── CLAUDE.md          (stays at root — Claude Code auto-loads)
├── README.md          (stays at root if exists)
└── run_pipeline.py    (stays at root — primary entry point)
```

### Delete candidates (need your OK)

**Duplicates / "Copy" files (safe, ~high confidence delete):**
- `override_final - Copy.png`
- `override_final_backup - Copy.png`
- `panel4_override_painter - Copy.py`
- `_debug_test.txt`
- `debug.txt`
- `filelist.txt`
- `preview_log.txt` (if not actively tailed)

**Legacy override PNGs (safer to archive than delete — they're history):**
- `override_final1.png`, `override_final_pre_vectorized.png`, `override_final_preriver.png`, `override_smoothed.png`, `override_vectorized.png`, `override_wip_pass1.png`, `override_wip_pass1_2.png`, `override_wip_zbackup.png`, `override_wip_zbackup2.png`
- Keep: `override_final.png` (master, CLAUDE.md says never modify), `override_base.png`, `override_final_backup.png`

**Stale validation report dirs (likely archive or delete after confirming):**
- `validation_report/`, `validation_report_band2/`, `validation_report_geo/`, `validation_report_geo_48_48/`, `validation_report_geo_50_46/`, `validation_report_geo_56_46/`, `validation_report_height_48_48/`, `validation_report_height_50_46/`, `validation_report_height_56_46/`, `validation_report_land/`, `validation_report_mixed/`
- Proposal: delete all 11. Rerun `validate_test_tile.py` fresh on tiles you care about.

### Diagnostics triage (35 scripts)

These are organized by CLAUDE.md Section 8 and by topic. I'd propose keeping the ones PROJECT_MEMORY explicitly lists as toolkit, archiving ones that look session-scoped.

**KEEP (listed in PROJECT_MEMORY diagnostic toolkit — still useful):**
- `diag_layers_breakdown.py` (referenced by CLAUDE.md for stratification fix)
- `diag_sand_rock_world.py`
- `diag_rock_staircase.py`
- `diag_rock_surface_topdown.py`
- `diag_floodplain_topdown.py`
- `diag_river_3x3_topdown.py`
- `diag_centerline_compare.py`
- `diag_river_path.py`

**LIKELY ARCHIVE (session-scoped exploration, safe to mothball):**
- `diag_desert_rivers_final.py`, `diag_desert_rivers_preview.py`, `diag_desert_rivers_south.py` (3 sibling probes — pick one to keep, archive others)
- `diag_floodplain_options.py`, `diag_floodplain_world.py` (superseded by `_topdown`?)
- `diag_lake_2panel.py`, `diag_lake_seam.py`, `diag_lake_shore_zoom.py`, `diag_lake_terrain_intersection.py`, `diag_lake_wl_sweep.py` (lake work concluded S30)
- `diag_river_lake_junction.py`, `diag_river_lake_junction2.py` (deferred per PROJECT_MEMORY §7.10)
- `diag_river_overview.py` (vs `diag_river_path.py`)
- `diag_tile_51_53.py`, `diag_tile_zoom.py` (tile-specific)
- `diag_meander_proto.py` (S34, shipped)
- `diag_find_crop.py`, `diag_global_overview.py`, `diag_inland_sea.py`, `diag_shallow_ocean.py`, `diag_ocean_depth.py`, `diag_land_height.py`, `diag_biome_rivers.py`, `diag_profile.py`, `diag_spline.py`, `diag_flow_threshold.py`, `diag_world_meander.py`

**DECISION NEEDED from you** on each. I don't know which you still open.

---

## Strategic "what's next" memo

You have three workstreams competing:

**A. The stated top priority: stratification rings bug.** CLAUDE.md names it. It's a known-localized fix in `_apply_desert_rock_palette()` step 6 with three pre-scoped candidate solutions. One focused session, maybe two.

**B. Clean-up-and-revamp (what you're asking about now).** Repo hygiene, move files, delete dupes, document commands, create skills. No visual improvement to the world.

**C. Agentic automation.** The 3×3 validator + skills + baseline regression harness. Enables you to let Claude Code run unattended and trust it didn't break things.

**My recommended order:**

1. **Do (A) first, this week.** Don't let cleanup block a bug fix that's already scoped. Ship the stratification fix in one session. Update PROJECT_MEMORY §5 with the resolution. This clears the top-of-CLAUDE.md blocker so every future session doesn't start with it in their face.

2. **Then the minimum viable cleanup** — a half-session pass, no big reorg:
   - Delete safe-duplicate list above (OK from you)
   - Delete 11 stale `validation_report_*` dirs (OK from you)
   - Add the "Commands" block to CLAUDE.md (I can draft it)
   - Do NOT move files into `scripts/` yet — it'll break every import and every `rebuild_*.py` invocation in your muscle memory. Defer to step 4.

3. **Then build the 3×3 validator and mask validator** (C, partial). This is the unblock for trustable autonomy. One focused session. Wraps existing validator, doesn't create new checks. ~300 lines.

4. **Then the file-move reorg** (B, full) — only after automation exists, because you'll want the validator to prove the reorg didn't break imports. Convert rebuild_*.py paths gradually: symlink old names to new locations during transition.

5. **Then draft the 5 skills** (C, rest). Each one uses the 3×3 validator internally, so it has to exist first.

6. **Then** pick up PROJECT_MEMORY §7 roadmap (snow cap aspect, windthrow orientation, etc.). These are the fun part.

**What I'd *not* do:** build `validate_heightmap/chunks/biomes.py` as separate scripts like the handoff doc suggests. Extend `validate_test_tile.py`'s check registry instead — it's already the right shape. Adding a new check is ~20 lines in one file vs a new module per concern.

**One question that will change the plan:** is the pipeline "done enough" that you're heading toward a full 50k generation run, or are you still in feature-add mode? If the former, skip (4) and (5) and prioritize a `validate_3x3.py` + baseline snapshots + a final 50k run plan. If the latter, the full sequence above makes sense.

---

## What I need from you to proceed

1. Approve/reject the delete list (duplicates + stale validation_report dirs).
2. Confirm whether I should draft the CLAUDE.md "Commands" section append.
3. Confirm: am I building `tools/validate_3x3.py` next, or are you tackling stratification rings first and I come back after?
4. Answer the "done enough for 50k run?" question above — it reorders everything.
