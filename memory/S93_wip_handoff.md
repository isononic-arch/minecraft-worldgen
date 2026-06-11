# S93 WIP HANDOFF — STALE (historical freeze only). The session SHIPPED: read [S93_handoff.md](S93_handoff.md) instead.
# Mystery resolved: (62,61) zero-rivers = migration RACE (shared tmp → rebuild fallback → OOM → regressed bed saved over v19), NOT the symmetric quantizer. Probe #3 proved monotone+v17 was always correct; symmetric hunk reverted. Race-safe migration + _BedCacheRefusal shipped in df0345f.

**Pushed state:** `0fce570` (S93 carver quantization, monotone form) — remote is clean through this.
**Local WIP commit on `s85-cherry-picks` (NOT pushed):** working tree at freeze = bowl warp + symmetric hysteresis + disabled headwater inset + v17→v19 cache migration. See "Working-tree contents" below.
**DO NOT render the 50k or any box from this tree until the (62,61) zero-rivers failure below is resolved.**

## THE CRITICAL DISCOVERY THIS SESSION (50k-blocker, independent of all WIP)
**`masks/_bed_cache_v17.pkl` is LOAD-BEARING and IRREPRODUCIBLE.** Rebuilding the bed cache from current code produces a DIFFERENT, SHALLOWER bed than the v17 pickle (v17 = 1.43GB, rebuild = 916MB; (62,61) stream depth 4-5 → 1; (27,34) estuary falls below the river-tag threshold and VANISHES). The cache key hashes paint+config only — code drift since the v17-era bed builder was never caught. Every render today that rebuilt the cache silently regressed the world's rivers. **Mitigation shipped in the WIP tree:** `_ensure_caches` migrates v17→v19 verbatim (adds `hw_inset_8k: None`, re-keys) instead of rebuilding — boxes self-heal (snapshot carries v17). **Never delete v17.** Reconciling the current bed builder vs the v17-era output = its own session (archaeology across S84-S91 bed changes).

## GATE STATE AT FREEZE (diag_s93f, rendered WITH migration + symmetric hysteresis)
- Migration fired ✓ ("bed cache MIGRATED v17 -> v19" in diag_s93f/render.log).
- **(62,61): RIVERS GONE (riv mask empty → skeleton percentile crash).** NEW failure, post-migration. The morning v17-based renders (diag_river_s93, monotone quantizer) had 4366 cells + correct depths... wait — the morning gate didn't measure (62,61) depth, but cells existed. PRIME SUSPECT: the **symmetric-hysteresis edit** (this gate's only new carver code) — possibly the upward re-anchor walks the scrambled lake-bound order up above banks → v8.14-class caps/water>bank interactions kill the rm tagging. SECOND suspect: migration's re-keyed v19 loads fine locally (verified earlier in a fresh process — `_hw_inset_8k_cache SET`, bed/width present) but maybe a field the carver needs differs. THIRD: check rm histogram first — rm may have cells with rwy ≤ 0 (water assignment died, tagging alive).
- (27,34)/(27,33)/(33,33) verdicts NOT yet computed for diag_s93f (the verdict script crashed on the (62,61) empty array before printing them — dumps are on disk, just re-run the battery with empty-guard).

## NEXT PROBE (first action after compact)
1. `rm` histogram + rwy range at (62,61) from diag_s93f (cells tagged? water values?). Compare against diag_river_s93 (morning, monotone quantizer, v17 cache, 4366 cells) — isolates symmetric-hysteresis vs migration.
2. If symmetric hysteresis is the killer: likely fix = re-anchor-on-rise is wrong (water_y jumping UP above banks); try: symmetric stepping WITHOUT upward re-anchor (up-steps only via the slow ≥0.5+MIN_RUN rule, no ≥1.5 up re-anchor), or cap `_lvl ≤ nearest_avg+bank_lift` per cell. Gate on (62,61) depth (TARGET p50 4 max 5) + cells (4366) + (27,34) incoherent=0 + hist clean + border 23/23 + (33,33) bowl arcs.
3. The monotone quantizer (pushed, `0fce570`) had clean (27,34) but UNKNOWN-at-the-time (62,61) depth collapse — RE-MEASURE (62,61) depth on diag_river_s93 dumps (they exist!) to know whether monotone-on-v17 was actually fine (if depth was 4-5 there, the whole symmetric change was chasing a cache regression that the migration already fixed → REVERT to monotone = simplest ship).
   ```
   py -c "import numpy as np; from scipy.ndimage import distance_transform_edt; from skimage.morphology import skeletonize; d='diag_river_s93'; rwy=np.load(f'{d}/rwy_62_61.npy').astype(np.int32); sy=np.load(f'{d}/sy_final_62_61.npy').astype(np.int32); rm=np.load(f'{d}/rmeta9_62_61.npy'); riv=((rm==1)|(rm==2))&(rwy>0); skel=skeletonize(riv); inside=distance_transform_edt(riv); sk=np.where(skel); print(riv.sum(), np.median((rwy-sy)[sk]), (rwy-sy)[sk].max())"
   ```
   NOTE: diag_river_s93 was rendered BEFORE the v19 rename → v17 cache → if depth there = 4-5, monotone+v17 is PROVEN GOOD.

## Working-tree contents at freeze (all compile; py_compile clean)
- `core/river_carver_v2.py`: (a) lake-bowl depth warp — depth field warped with the same world-coord displacement as the shoreline; VALIDATED (terraces arc, gates b/d images diag_s93b/bowl_terraces_33_33.png + diag_s93d/bowl_final_compare.png); (b) symmetric-hysteresis quantizer — SUSPECT, see above.
- `core/hydro_region_overlay.py`: v19 cache name; v17→v19 verbatim migration (GOOD, keep); S93 headwater-inset machinery behind `_HW_INSET_ENABLED=False` (flag-off proven no-op; the inset needs the carver width-map session — rendered width is NOT a single-lever function of the carve SDF: centerline+EDT-width radius, polygon SDF, carve thresholds, bed override all contribute; two gates gave internally inconsistent width/depth responses).
- `tools/diag_river_bed_depth.py`: v19 pointer.
- `memory/S89_*.md`, `memory/verify_tiles.txt`: pre-session dirt (NOT ours, leave).

## User-visible state / what the user knows
- User walked + approved: carver quantization (estuary clean at (27,34)), tree-border substitution. Both pushed.
- User asked for: (1) headwater thin→wide (parked, flag off, needs width-map session — user knows "we can implement" was attempted; report the inset findings honestly); (2) lake-bowl step-lines de-straightened (DONE pending their walk — confirm-back was confirmed correct); ship ASAP.
- Tokens: two Hetzner tokens burned in chat (user will rotate). No boxes alive. $0.

## SHIP PATH (probable, pending probe #3)
If monotone+v17 proves good: revert the symmetric-hysteresis hunk (keep bowl warp + migration + flag-off inset), re-gate the 4 tiles once (warm v19 ≈ 12 min), commit "S93b: bowl warp + bed-cache v17 migration", push, install (33,33)+(62,61)+(27,34) to Vandir50k_verify for the user's walk, write S93 handoff + CLAUDE.md current-state. The 50k then needs: boxes migrate v17 (automatic) — verify the MIGRATED line in box logs before the full run.
