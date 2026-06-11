# S93 HANDOFF (final) — carver quantization, tree-lane substitution, bowl warp, BED-CACHE MIGRATION

**Sessions S93 + S93b (2026-06-10/11), branch `s85-cherry-picks`, ship commit `df0345f` on top of WIP freeze `06145ee` on top of pushed `0fce570`.**
Supersedes [S93_wip_handoff.md](S93_wip_handoff.md) (mid-bisect freeze — historical only).

## WHAT SHIPPED (all walked or gate-passed)

1. **Tree-border lane fix (`0f5ffe1`, user-walked "its fine")** — S91 edge-guard's clipped placements are now SUBSTITUTED: at edge candidates whose schematic extent can't fit, re-roll weighted among entries that DO fit (instead of dropping). Fills the structural border trough left by corner-anchored one-sided extents.
2. **Carver monotone water-y quantization (`0fce570`, user-walked "Seems good… good work")** — per-path σ=8 smoothing then MONOTONE integer quantization with hysteresis (MIN_RUN=12, re-anchor on ≥1.5 drops) along dist-from-ocean order; kills the checkered/floating water at elevation steps ((27,34) incoherent 34→0, water hist {63,64} only, border 23/23).
3. **Lake-bowl depth warp (`df0345f`, PENDING USER WALK)** — the bowl's depth field is `map_coordinates`-displaced with the SAME world-coord simplex warp as the shoreline SDF, so bed terraces arc with the shore instead of long straight x/z ledges. True A/B at (62,61): 83,948 lake-bed cells moved ±1–3, only 79 cells elsewhere. Visual: `diag_s93g/bowl_compare_62_61.png`.
4. **Bed-cache v17→v19 verbatim migration, race-safe (`df0345f`)** — see CRITICAL section below.

## CRITICAL: THE BED CACHE (50k-blocker knowledge)

- **`masks/_bed_cache_v17.pkl` (1.43GB) is LOAD-BEARING and IRREPRODUCIBLE. NEVER delete it.** The current bed builder produces a DIFFERENT, SHALLOWER bed ((62,61) stream depth 4-5 → 1; the (27,34) estuary drops below the river-tag threshold and VANISHES). Code drifted since the v17-era builder; the cache key (paint + `_ensure_caches` source + tunables) never caught it. Reconciling the builder vs v17 output = its own session.
- `_load_bed_cache_from_disk` now MIGRATES v17→v19 verbatim (re-keyed, + `hw_inset_8k: None`) instead of rebuilding. Race-safe: per-PID tmp names; losing workers wait for the winner's v19; **any rebuild while v17 exists raises `_BedCacheRefusal` (fatal)** — including the stale-key path, which re-migrates instead (key hashes `_ensure_caches` SOURCE → any code edit there flips v19 stale; without the refusal that would have silently regressed every river again).
- **The S93b freeze-gate failure decoded:** 4 workers raced the migration on a shared tmp → 3 PermissionError → fell back to rebuild → one OOM'd (`global bed precompute skipped: MemoryError`) → (62,61) rendered ZERO rivers → a rebuild worker SAVED its regressed 1.1GB bed over the good migrated v19. The symmetric-hysteresis quantizer was wrongly suspected; probe on the morning monotone+v17 dumps proved depth was always correct (4366 cells, p50 4 / max 5) → symmetric hunk reverted to pushed monotone.
- **Before any 50k/box run: verify the box log shows `bed cache MIGRATED v17 -> v19` (or `bed cache HIT`) and NEVER `falling back to rebuild` (string removed) or `_BedCacheRefusal`.** Boxes self-heal (snapshot ships v17).
- Healthy sizes: v17 = 1,429,741,075 B; migrated v19 ≈ 1,429,741,090 B. A ~1.1GB v19 is a poisoned rebuild → delete v19, re-warm (single process: `_load_bed_cache_from_disk(Path('masks/hydro_region.png'))`).

## LOCAL RENDER OPS LESSONS
- **4 workers OOM this machine** (8k² float64 allocs in tile hydro). Use `--threads 2` (or 1) for local multi-tile renders; warm the v19 single-process first. Pipeline EXITS 0 even when a tile errors — grep the log for `tile_error` before trusting outputs.
- Verdict battery + gates for this work: `diag_s93g/verdict.py` (scratch), `tools/diag_water_coherence.py`, `tools/diag_bowl_compare.py`, `tools/diag_border_topdown.py`.

## GATE STATE AT SHIP (diag_s93g, warm verbatim v19)
- (62,61): 4366 river cells, skel depth p50 4 / max 5, rwy flat 95, incoherent 0, floating 0; rwy byte-identical to approved morning render; bowl warp arcs.
- (27,34): byte-identical to the user-approved morning estuary render (floating=34 is a pre-existing approved metric quirk at the mouth).
- (27,33): 363 cells, coherent, flat 64; border vs (27,34): presence 23/23, water-Y 23/23.
- (33,33): lake 112,697 cells; bed identical to validated s93d bowl state.

## INSTALLED FOR USER WALK (Vandir50k_verify)
| Tile | What to check | TP |
|---|---|---|
| (33,33) | big lake bowl — terraces should arc/wobble, no straight x/z ledges | `/tp @s 17006 98 17194` |
| (62,61) | lake junction bowl + stream depth (4-5) intact | lake `/tp @s 32044 115 31412`, stream `/tp @s 31921 115 31677` |
| (27,34) | estuary (already approved — regression sentinel) | `/tp @s 14249 83 17462` |
| (27,33)\|(27,34) | seam at z=17408 — river crosses cleanly | `/tp @s 14249 83 17408` |

## PARKED / NEXT
- **Headwater thin→wide:** parked behind `_HW_INSET_ENABLED=False` in `hydro_region_overlay.py`. Rendered width is NOT a single-lever function of the carve SDF (centerline+EDT width radius, polygon SDF, carve thresholds 0.1/0.5, bed override all contribute; two gates gave inconsistent width/depth responses). Needs a dedicated carver width-map session: trace centerline → EDT-width → polygon-SDF → thresholds → bed-override end-to-end FIRST, then re-attempt the inset.
- **Bed-builder reconciliation vs v17 output** (own session; until then the migration is the safety).
- Both Hetzner tokens burned in chat — user to rotate. No boxes alive ($0).
