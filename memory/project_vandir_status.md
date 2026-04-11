# Vandir Project Status — Validator Fix Log

## 2026-04-10 — `tools/validate_masks.py` corner-sampling bug fix

**Symptom:** Smoke test step 1 reported 13 PASS / 7 FAIL. All 7 failures were `coverage 0.000 < min`. Affected masks: `override`, `shore`, `river`, `hydro_floodplain`, `wind_windthrow`, `rock_exposure`, `sand_dunes`.

**Root cause:** `tools/validate_masks.py` sampled coverage from a single 1024×1024 window at the NW corner via `Window(0, 0, w, h)`. The NW corner of Vandir is open ocean, so any mask whose content is land-only (override zones, shore, rivers, eco gradients, dunes) read as all-zero in that window and tripped its `min_cov` floor.

**Verification before fix:** Read 2000×2000 windows centered at (25000, 25000) via rasterio for each "failing" mask. Six of seven had real data in the center (`override` 64.55% nonzero, 4 zone codes; `shore` 36.90%; `river` 0.36%; `hydro_floodplain` 4.46%; `wind_windthrow` 0.23%; `rock_exposure` 1.75%). Only `sand_dunes` was actually empty in the center window — needs separate follow-up.

**Fix:** Replaced the corner `Window(0, 0, w, h)` read with a strided full-raster downsample using `src.read(1, out_shape=(2048, 2048), resampling=Resampling.nearest)`. This gives a true global coverage estimate at ~one 4 MB transfer per mask, uses overviews if present, and preserves discrete zone codes / binary masks under nearest resampling. Renamed `SAMPLE_WINDOW` constant to `SAMPLE_GRID = 2048`. Dropped the now-unused `from rasterio.windows import Window` import; added `from rasterio.enums import Resampling`.

**Files touched:** `tools/validate_masks.py` only. No `core/`, `masks/`, `override*`, `rebuild_*` modifications (per smoke-test hard rules).

**Next:** Re-run `py tools/validate_masks.py --masks masks/ --report validation_report_masks` and only proceed to `validate_3x3.py` if all PASS.

## 2026-04-10 — `validate_3x3.py` ran 1h27m with no output, killed at user request

Tile (36, 20). Started 15:14, killed 16:42. Process active (~26 min CPU, 1.1 GB RSS) but produced **zero stdout and zero report files** — `validation_report_3x3_36_20/` was empty at kill time. Stdout file contained only two `NotGeoreferencedWarning` lines. Smoke-test budget for this step was 15–25 min, so 1h27m was a real anomaly.

Likely causes (unverified):
1. Python stdout buffering hiding all progress markers — try `PYTHONUNBUFFERED=1` next run.
2. Real hang in pipeline runner at tile (36, 20) — needs py-spy dump if it reproduces.
3. Validator entry point may set up state per-tile 9× without sharing caches.

**Next after mask validator passes:** rerun `validate_3x3.py` with `PYTHONUNBUFFERED=1` so we get progress visibility. If it still goes silent past the 25 min budget, capture a py-spy dump rather than killing blind.

## 2026-04-10 — validator bound fix (override + shore) + polarity inversion in triage doc

**Symptom (post corner-sampling fix):** Re-run of `validate_masks.py` reported 18 PASS / 2 FAIL:
- `override.tif`: `coverage 0.399 < min 0.500 (discrete)`
- `shore.tif`: `coverage 0.300 > max 0.150 (gradient)`

Runtime triage (`TRIAGE_validate_2026-04-10.md`, written by Claude Code) concluded "30% of land is unzoned" and flagged the failures as a real Sessions-20-24-style regression.

**Root cause 1 — polarity inversion in triage doc:** The triage measured land using `height ≤ 17050` (labeling it "land fraction: 70.00%"). This is **inverted**. Canonical rule (`CLAUDE.md` HEIGHT POLARITY, `PROJECT_MEMORY.md` §S20): HIGH raw 16-bit = HIGH terrain, sea level = raw 17050, so **land = `height > 17050`**. Correct land fraction = 30.00%.

Once the polarity is right, both failures become validator-default bound bugs, not pipeline regressions.

**Root cause 2 — `override` bound wrong:** I wrote `min_cov: 0.50` assuming override should cover ~50%+ of the world. Correct expectation: override nonzero ≈ 40% globally = land × 99.6% + ~14% of ocean (coastal/marine zones). Current measurement:
```
land (height > 17050)   = 30.00%
override nz (world)     = 39.90%
override nz over land   = 99.63%    ← healthy
override nz over ocean  = 14.30%    ← intentional (mangrove, kelp, etc.)
land that is unzoned    =  0.37%    ← thin waterline, expected
```

**Root cause 3 — `shore` bound wrong:** I modeled `shore.tif` as a thin coastline band (`gradient, max_cov: 0.15`). It's actually a **binary land mask**: 65535 on land, 0 on ocean, matching `(height > 17050)` pixel-for-pixel (verified: `diff = 0` at 2048×2048 strided read). File mtime is 2026-03-03 — predates all recent sessions, pipeline has been consuming it as a land mask for weeks. Nothing in `core/` treats it as a band.

**Source-of-truth verification:** Rendered `masks/override.tif` at 1024×1024 with hash palette, compared against `output/override_worldview.png` (Apr 6 reference). Both show the same four landmasses with full biome coverage, no unpainted tracts.

**Fix:**
- `config/validation_affects.json` mask_bounds: `override` → `{discrete, 0.35, 0.50}`, `shore` → `{discrete, 0.25, 0.40}`.
- `tools/validate_masks.py` `DEFAULT_BOUNDS` fallback: same values, plus inline comments warning against raising `min_cov` without re-checking against `output/override_worldview.png` and reminding that land fraction is 30% not 70%. Fixed the stale docstring example at line 31 as well.
- `TRIAGE_validate_2026-04-10.md`: added a polarity-correction banner at the top pointing at the source-of-truth reference image. Original runtime analysis preserved below the banner.

**Files touched:** `config/validation_affects.json`, `tools/validate_masks.py`, `TRIAGE_validate_2026-04-10.md`, this file. **No** `core/`, `masks/`, `override*`, or `rebuild_*` modifications.

**Incidental fix — truncated `validate_masks.py`:** during this pass cowork discovered `tools/validate_masks.py` was truncated at line 252 with an unclosed `write_text(` call; `main()` had no return and no `if __name__ == "__main__"` block. `python3 -m py_compile` failed. This means Claude Code's corner-sampling fix save earlier in the session was incomplete and any PASS/FAIL output reported from it came from a stale cached run or a partial save that doesn't match disk. Completed `main()` with JSON checks.json writer, `return 0 if n_fail == 0 else 1`, and standard `sys.exit(main())` entry point. File now compiles clean.

**What this does NOT catch (filed as TODO against the `tests/baselines/` system):** per-zone distribution shifts. A regression that keeps total coverage in band but scrambles zone-mix (e.g. zone 120 dropping from 17% to 1% of land) will still pass. Follow-up: when baseline-diff infrastructure exists for `validate_3x3.py`, add a companion per-zone histogram snapshot for `override.tif` at the same snapshot point.

**Persistent doc hygiene note:** the polarity inversion has bitten this project multiple times (S20 biome_assignment bug, this triage doc). Canonical docs (`CLAUDE.md`, `PROJECT_MEMORY.md`, `ARCHITECTURE_VISION.md`) all state polarity correctly. The inversion lives only in runtime triage notes when a session loses the thread. Source of truth for override correctness going forward: **visual match against `output/override_worldview.png`**, not numerical guesses about "what % of land should be unzoned".

**Next:** re-run `py tools/validate_masks.py --masks masks/ --report validation_report_masks`. Expected: 20 PASS, 0 FAIL. Then proceed to `validate_3x3.py` with `PYTHONUNBUFFERED=1`.

## 2026-04-10 — validate_3x3 (48,48) smoke + validator scope gating for _OCEAN

**Run:** `validate_3x3.py --tile-x 48 --tile-z 48` with `PYTHONUNBUFFERED=1`. Center sea tile. Completed in ~16 min (ocean-heavy, per-tile ~24-60s). Result: **73 PASS / 4 FAIL / 7 WARN** across 9 tiles + 3 seam checks.

**FAILs (all 4 same class — validator scope bug, not a pipeline regression):**
- `no_bare_dirt_surface`: reported dirt pixels on ocean-floor columns (seafloor sediment rendered as `dirt` before ocean column pass). Check was running against the full grid including `_OCEAN` columns.
- `surface_block_variety`: tiles that are ~100% ocean have low block variety by definition (water + a handful of seafloor blocks); tripped the `n < K` floor.

**Q3 parked observation — rivers-in-ocean visual:** Nick eyeballed `stitched_surface_y.png` and confirmed the faint groove matches the Gaea contour for that region. `surface_y` is smooth; only `stitched_blocks.png` shows faint river tracks where the palette paints water/sand through ocean columns. Not a pipeline bug, not worth chasing now. Filed as benign. If it ever matters, the fix would live in `core/column_generator.py` river-below-sea suppression, not in the validator.

**Fix — scope-gate both checks to land/non-ocean:**
- `tools/validate_test_tile.py::chk_no_bare_dirt_surface` (line ~87): now takes `biome_grid`, builds `land_mask = biome_grid != "_OCEAN"`, counts dirt only where `dirt_mask & land_mask`. Message updated to "…(land only)".
- `tools/validate_test_tile.py::chk_surface_block_variety` (line ~214): now takes optional `biome_grid`. If `(biome_grid == "_OCEAN").mean() > 0.95`, returns PASS with message `skipped (ocean-heavy tile, N% _OCEAN)`. Land tiles fall through to the existing variety floor.
- Call sites updated: `validate_test_tile.py:628` and `validate_3x3.py:341` now pass `biome_grid` / `art.biome_grid`.
- Both files compile clean under `py -m py_compile`.

**Per-tile elapsed_ms reference (from (48,48) run, recorded for future budget tuning):** deep ocean tile ~24s, ocean+rivers tile ~60s, mostly-land tile 200-320s. 9-tile 3×3: ocean center ~16 min actual; land-heavy center like (36,20)/(24,80)/(25,72) → budget **60 min**, hard ceiling **90 min** before suspecting a hang. Added to `CLAUDE.md` validate_3x3 command block along with the `PYTHONUNBUFFERED=1` requirement.

**Files touched:** `tools/validate_test_tile.py`, `tools/validate_3x3.py`, `CLAUDE.md`, this file. **No** `core/`, `masks/`, `override*`, `rebuild_*` touched.

**Next:** re-run `PYTHONUNBUFFERED=1 py tools/validate_3x3.py --config config/thresholds.json --masks masks/ --output output/ --tile-x 48 --tile-z 48 --report validation_report_3x3_48_48`. Expected: **77 PASS / 0 FAIL / 7 WARN**. If clean → ask Nick before snapshotting `tests/baselines/3x3/48_48/`.

## 2026-04-10 — validator memory optimization + two 3×3 baselines

**Memory strip in `validate_3x3.py`:** after `run_single_tile_checks(art)` completes for each tile, we now null out heavy buffers before storing the artifact for seam stitching — `col_results=[]`, `masks={}`, `pre_carve_y/river_meta/eco_grads/cliff_deg/sub_blk/ground_cover=None`, `placements=[]`. Only `biome_grid`, `surface_blk`, `surface_y` are retained (the three inputs to `stitch_3x3`). Root cause of the prior footprint: the 262144 `ColumnResult` tuples each carrying a per-column `blocks: dict[int,str]` are the peak allocation, and we were holding all 9 in memory simultaneously for no downstream use.

**Measured impact:** re-ran (48,48) center-sea tile — **79 PASS / 0 FAIL / 5 WARN in 610s** vs. prior 958s = 36% wall-time speedup on the same inputs. Bigger than expected; likely a combination of reduced GC pressure and more page cache available for rasterio mask reads.

**Baseline snapshots committed:**
- `tests/baselines/3x3/48_48/` — center-sea reference. 79 PASS / 0 FAIL / 5 WARN. 5 WARNs are `river_meta_consistency: No river pixels in this tile` for the pure-ocean interior tiles — expected and benign. This baseline guards the ocean/coast/seam path.
- `tests/baselines/3x3/51_53/` — floodplain/lakes/riparian reference. **74 PASS / 8 FAIL / 2 WARN.** Committed as a "baseline with known failures" — the `--baseline` diff only flags PASS→FAIL flips, so this still catches new regressions on the river/lake/forest path.

**Known failure in the (51,53) baseline — riparian bare-dirt hole (pre-existing, NOT a regression):**
- 7 of 9 tiles FAIL `no_bare_dirt_surface` with total 53,311 bare-dirt pixels on land (~2.3% of land surface in the 3×3).
- Strong linear correlation with river pixel count: ~10–12% of river pixels → bare dirt in the final surface block grid. Clean tiles (50,52) and (52,52) have zero rivers AND zero bare dirt; every river-bearing tile has bare dirt.
- Zone codes in the window (sampled from `masks/override.tif` directly): **zone 120 MIXED_FOREST = 85%**, **zone 20 TEMPERATE_RAINFOREST = 15%**. The hunt is narrowed to these two biomes' riparian-band handlers in `core/surface_decorator.py`. MIXED_FOREST is the likely primary suspect given its coverage.
- Also 1 FAIL on `biome_seam_continuity` (6.25× interior transition rate). Visual inspection of `stitched_biomes.png` shows a natural terrain-following boundary between the two biomes; likely false positive from the seam check's simple interior-vs-edge ratio when a real biome line crosses multiple tile rows. Low priority.
- Why this wasn't caught before: Session 15 validation was eyeball-only in-game. The new `no_bare_dirt_surface` check in `validate_test_tile.py` is the first automated counter of surface block identity on land. This is a preexisting issue the validator surfaced, not something a recent edit broke.
- 2 WARNs on `river_meta_consistency` for (50,52) and (52,52) — both are headwater ridges with zero rivers, expected.

**Status of the bare-dirt bug:** ~~PARKED. Not investigating now per Nick's direction. When a future session touches `core/surface_decorator.py` for anything adjacent, pull this log and hunt the MIXED_FOREST / TEMPERATE_RAINFOREST riparian handler first. When fixed, rerun (51,53) and replace the baseline with a cleaner one.~~ **STRICKEN 2026-04-10 (S43) — user confirmed this is a validator false positive, not a real bug. See S43 correction entry below.**

**Files touched this entry:** `tools/validate_3x3.py` (memory strip), `tests/baselines/3x3/48_48/*`, `tests/baselines/3x3/51_53/*`, this file. **No** `core/`, `masks/`, `override*`, `rebuild_*` touched.

## 2026-04-10 — Session 42 close-out

**Landed this session:**
1. `_OCEAN` scope gates on `chk_no_bare_dirt_surface` and `chk_surface_block_variety` in `tools/validate_test_tile.py` + call-site updates in `tools/validate_3x3.py`. Eliminated 4 false-positive FAILs + 2 false WARNs on ocean tiles.
2. Memory strip in `tools/validate_3x3.py`: after per-tile checks run, null `col_results` / `masks` / `pre_carve_y` / `river_meta` / `eco_grads` / `cliff_deg` / `sub_blk` / `ground_cover` / `placements`. Keep only `biome_grid` / `surface_blk` / `surface_y` for seam stitching. Measured impact: (48,48) re-run 958s → 610s (−36%).
3. Two 3×3 baselines committed:
   - `tests/baselines/3x3/48_48/` — 79/0/5, clean, ocean/coast/seams.
   - `tests/baselines/3x3/51_53/` — 74/8/2, committed as baseline-with-known-failures for rivers/lakes/mixed forest.
4. `CLAUDE.md` updated: wall-time budget table revised with real per-tile data (deep ocean ~24s, ocean+rivers ~60s, mostly-land 200-320s); `PYTHONUNBUFFERED=1` elevated to mandatory; new "Baseline before editing new code paths" workflow rule with current baseline inventory and uncovered-tiles list.
5. `PROJECT_MEMORY.md` updated: S42 entry added to §10 session log; §7 next-session priority reordered (riparian bare-dirt hole #1, stratification #2); §5 solved-problems index updated for mask validator resolution; §11 P0 entries for "iteration wall time" and "no regression baseline" marked largely/partially resolved with status.

**Parked for future sessions (all logged, no action needed now):**
- ~~Riparian bare-dirt hole in MIXED_FOREST / TEMPERATE_RAINFOREST~~ **STRICKEN S43 — validator false positive, not a real bug.**
- Stratification rings bug (downgraded by user: "not that big"). Still listed in CLAUDE.md as lower priority under DIRECTION #3.
- Q3 rivers-in-ocean visual (benign, surface_y is smooth; only block palette shows faint tracks).
- Per-zone distribution diffs on baselines (a regression scrambling zone mix but preserving total coverage would still pass).

**Workflow invariants held this session:** no `core/`, `masks/`, `override*`, `rebuild_*.py` touched. No failed fix loops. All validator rescoping was on the check-side, not the pipeline-side. No 50k runs suggested or considered.

**Ready state for next session:** open agenda. User picks from CLAUDE.md TOP PRIORITY. Two baselines in place guard ocean/coast/seams + rivers/lakes/mixed forest paths against regression. Validator memory footprint fits 8 GB comfortably. Land-heavy 3×3 budget is 20-60 min wall time with PYTHONUNBUFFERED=1.

## 2026-04-10 — Session 43 opening: strike riparian bare-dirt false positive

**Correction:** Nick confirmed the "riparian bare-dirt hole" logged in the S42 entries above is a **validator false positive**, not a real bug. The 53,311 "bare dirt" pixels reported by `chk_no_bare_dirt_surface` on the (51,53) baseline do not correspond to any in-world visual issue. The S42 log was written in good faith off the validator output; the validator itself is what's wrong here.

**What this means going forward:**
- The (51,53) baseline stays committed as-is. Its 8 pre-existing FAILs (7× `no_bare_dirt_surface` + 1× `biome_seam_continuity`) are now a **locked known-state**, not a debugging target. `--baseline tests/baselines/3x3/51_53` still correctly flags new PASS→FAIL flips, which is the only reason we kept it.
- Do **not** hunt the MIXED_FOREST / TEMPERATE_RAINFOREST riparian handlers in `core/surface_decorator.py` based on that S42 log. That trail is cold.
- Open question deferred: is `chk_no_bare_dirt_surface` itself wrong (counts intentional riparian dirt as a bug), or is it right but the signal is drowned out by something else? Not investigating now — filed under "validator audit" for whenever we next do a check-side sweep.

**Direction set for S43:** physical-realism revamp of surface block painting + subsurface geology, driven by the `Realistic World Examples/` reference images. CLAUDE.md DIRECTION block rewritten accordingly. Entering discovery/interview phase with user before writing any plan.

**Files touched this entry:** `CLAUDE.md` (TOP PRIORITY → DIRECTION block rewrite), this file. **No** `core/`, `masks/`, `override*`, `rebuild_*` modifications.
